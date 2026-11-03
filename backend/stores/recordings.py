"""Shared SQLite persistence for OHLCV candle recordings.

One implementation backs every recordings database: the stores differ only in
database path and schema variant, never in row semantics.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence

# One candle per time bucket, highest-id row wins: recorders that used
# INSERT OR REPLACE on a table without a UNIQUE constraint left one row
# per tick, and the last row written holds the most complete OHLCV state.
_DEDUPE_CANDLES_SQL = """
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
        """

# Recordings whose distinct-bucket count falls below the threshold, counting
# only rows that actually exist in the candles table.
_CLEANUP_SMALL_SQL = """
        SELECT r.id
        FROM recordings r
        LEFT JOIN (
            SELECT recording_id, COUNT(DISTINCT time) as cnt
            FROM candles
            GROUP BY recording_id
        ) c ON r.id = c.recording_id
        WHERE COALESCE(c.cnt, r.candle_count, 0) < ?
        """

_RECORDING_BASE_COLUMNS = ("mint", "token_name", "token_symbol", "timeframe", "started_at")


@dataclass(frozen=True)
class StoreSchema:
    """A database schema variant: one init script plus idempotent migrations.

    Each migration is ``(sql, ignore_errors)``; ``ignore_errors`` marks the
    additive migrations that must no-op when the column already exists.
    """

    init_sql: str
    migrations: tuple[tuple[str, bool], ...] = ()


class RecordingStore:
    """SQLite store for recordings and candles, parameterised by database path."""

    def __init__(self, db_path: Path, schema: StoreSchema) -> None:
        self.db_path = db_path
        self.schema = schema

    def _write_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _read_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        return conn

    def init_db(self) -> None:
        """Create the schema and apply the variant's migrations."""
        conn = self._write_conn()
        conn.executescript(self.schema.init_sql)
        for sql, ignore_errors in self.schema.migrations:
            try:
                conn.execute(sql)
            except Exception:
                if not ignore_errors:
                    raise
        conn.commit()
        conn.close()

    def create_recording(
        self,
        mint: str,
        timeframe: str,
        token_name: str,
        token_symbol: str,
        *,
        extra_columns: Sequence[str] = (),
        extra_values: Sequence[object] = (),
        started_at: float | None = None,
    ) -> int:
        """Insert one recording row and return its id."""
        columns = list(_RECORDING_BASE_COLUMNS) + list(extra_columns)
        placeholders = ", ".join("?" for _ in columns)
        sql = f"INSERT INTO recordings ({', '.join(columns)}) VALUES ({placeholders})"
        started = time.time() if started_at is None else float(started_at)
        conn = self._write_conn()
        cur = conn.execute(sql, (mint, token_name, token_symbol, timeframe, started, *extra_values))
        rec_id = cur.lastrowid
        conn.commit()
        conn.close()
        return rec_id

    def stop_recording(self, recording_id: int) -> None:
        """Stamp the stop time, persist the candle count, and mark the recording completed."""
        conn = self._write_conn()
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
        self,
        recording_id: int, t: int,
        o: float, h: float, l: float, c: float,
        vol: float,
        buy_vol: float = 0.0,
        sell_vol: float = 0.0,
        pool_sol: float = 0.0,
        market_cap_usd: float = 0.0,
    ) -> None:
        """Upsert one candle row: the previous row for the same bucket is deleted first."""
        conn = self._write_conn()
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

    def insert_candles_batch(self, recording_id: int, candles: list[dict]) -> None:
        """Insert many candle rows with INSERT OR REPLACE semantics."""
        conn = self._write_conn()
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

    def list_recordings(self) -> list[dict]:
        """Return every recording, newest first."""
        conn = self._read_conn()
        rows = conn.execute("SELECT * FROM recordings ORDER BY started_at DESC").fetchall()
        conn.close()
        return [dict(r) for r in rows]

    def get_recording(self, recording_id: int) -> Optional[dict]:
        """Return one recording row, or None when the id is unknown."""
        conn = self._read_conn()
        row = conn.execute("SELECT * FROM recordings WHERE id = ?", (recording_id,)).fetchone()
        conn.close()
        return dict(row) if row else None

    def get_recording_candles(self, recording_id: int, effective_basis: bool = False) -> list[dict]:
        """Return one completed candle per time bucket, ordered by time.

        With ``effective_basis``, PumpSwap-era prices stored as the raw vault
        ratio are lifted to the executable basis at load time, using each
        candle's stored vault depth; recordings already on that basis pass
        through untouched.  A failed basis correction never fails the load.
        """
        conn = self._read_conn()
        rows = conn.execute(_DEDUPE_CANDLES_SQL, (recording_id,)).fetchall()
        rec_row = None
        if effective_basis:
            rec_row = conn.execute(
                "SELECT mint, started_at FROM recordings WHERE id = ?", (recording_id,)
            ).fetchone()
        conn.close()
        candles = [dict(r) for r in rows]
        if effective_basis and rec_row is not None:
            try:
                from pool_virtual_reserves import correct_candles_to_effective_basis
                correct_candles_to_effective_basis(
                    candles, rec_row["mint"], float(rec_row["started_at"] or 0.0))
            except Exception:
                pass  # never fail a replay because the basis fix is unavailable
        return candles

    def delete_recording(self, recording_id: int) -> None:
        """Delete one recording and its candles."""
        conn = self._write_conn()
        conn.execute("DELETE FROM candles WHERE recording_id = ?", (recording_id,))
        conn.execute("DELETE FROM recordings WHERE id = ?", (recording_id,))
        conn.commit()
        conn.close()

    def update_recording_candle_count(self, recording_id: int) -> None:
        """Recount a recording's candles and persist the count."""
        conn = self._write_conn()
        count = conn.execute(
            "SELECT COUNT(*) FROM candles WHERE recording_id = ?", (recording_id,)
        ).fetchone()[0]
        conn.execute("UPDATE recordings SET candle_count = ? WHERE id = ?", (count, recording_id))
        conn.commit()
        conn.close()

    def cleanup_small_recordings(self, min_candles: int) -> int:
        """Delete recordings with fewer than ``min_candles`` candles and return the count."""
        conn = self._write_conn()
        rows = conn.execute(_CLEANUP_SMALL_SQL, (min_candles,)).fetchall()
        deleted_ids = [row[0] for row in rows]
        if deleted_ids:
            placeholders = ",".join("?" * len(deleted_ids))
            conn.execute(f"DELETE FROM candles WHERE recording_id IN ({placeholders})", deleted_ids)
            conn.execute(f"DELETE FROM recordings WHERE id IN ({placeholders})", deleted_ids)
            conn.commit()
        conn.close()
        return len(deleted_ids)
