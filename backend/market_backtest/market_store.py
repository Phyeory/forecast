"""market_store.py — SQLite persistence for the market backtest systems.

A dedicated store (`backend/data/market_data.db`) that never touches the
memecoin tape databases (`price_data.db` / `candles.db` — invariant 8).
Schema mirrors `data_store.py`'s candles shape so the replay loop and the
calibration estimators consume identical row shapes.

Markets
-------
  "stocks"       US equities, daily adjusted bars (Nasdaq quote/chart API)
  "crypto_perp"  Binance USDT-M perpetual futures klines (Binance Vision)
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Optional

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
MARKET_DB = DATA_DIR / "market_data.db"


def _conn(readonly: bool = False) -> sqlite3.Connection:
    if readonly:
        conn = sqlite3.connect(f"file:{MARKET_DB}?mode=ro", uri=True)
    else:
        conn = sqlite3.connect(str(MARKET_DB), timeout=30.0)
        conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    return conn


def init_market_db() -> None:
    conn = _conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS market_recordings (
            id           INTEGER PRIMARY KEY,
            market       TEXT NOT NULL,
            symbol       TEXT NOT NULL,
            interval     TEXT NOT NULL,
            first_ts     REAL DEFAULT 0,
            last_ts      REAL DEFAULT 0,
            candle_count INTEGER DEFAULT 0,
            bars_meta    TEXT DEFAULT '{}',
            status       TEXT DEFAULT 'completed',
            fetched_at   REAL,
            UNIQUE(market, symbol, interval)
        );
        CREATE TABLE IF NOT EXISTS market_candles (
            recording_id   INTEGER NOT NULL,
            time           INTEGER NOT NULL,
            open           REAL NOT NULL,
            high           REAL NOT NULL,
            low            REAL NOT NULL,
            close          REAL NOT NULL,
            volume         REAL DEFAULT 0,
            buy_volume     REAL DEFAULT 0,
            sell_volume    REAL DEFAULT 0,
            pool_sol       REAL DEFAULT 0,
            market_cap_usd REAL DEFAULT 0,
            PRIMARY KEY (recording_id, time)
        );
        CREATE INDEX IF NOT EXISTS idx_mcandles_rec_time
            ON market_candles(recording_id, time);
    """)
    conn.commit()
    conn.close()


def upsert_market_recording(
    market: str,
    symbol: str,
    interval: str,
    meta: Optional[dict] = None,
) -> int:
    """Insert-or-update the recording row; returns its id."""
    conn = _conn()
    cur = conn.execute(
        "SELECT id FROM market_recordings WHERE market=? AND symbol=? AND interval=?",
        (market, symbol, interval),
    )
    row = cur.fetchone()
    if row:
        rec_id = row["id"]
        conn.execute(
            "UPDATE market_recordings SET fetched_at=? WHERE id=?",
            (time.time(), rec_id),
        )
    else:
        cur = conn.execute(
            "INSERT INTO market_recordings"
            " (market, symbol, interval, bars_meta, fetched_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (market, symbol, interval, json.dumps(meta or {}), time.time()),
        )
        rec_id = cur.lastrowid
    conn.commit()
    conn.close()
    return rec_id


def set_recording_bounds(rec_id: int, first_ts: float, last_ts: float,
                         candle_count: int, status: str = "completed",
                         meta: Optional[dict] = None) -> None:
    conn = _conn()
    conn.execute(
        "UPDATE market_recordings SET first_ts=?, last_ts=?, candle_count=?,"
        " status=?, bars_meta=? WHERE id=?",
        (first_ts, last_ts, candle_count, status,
         json.dumps(meta or {}), rec_id),
    )
    conn.commit()
    conn.close()


def insert_market_candles_batch(rec_id: int, candles: list[dict]) -> None:
    """Replace-all write of a symbol's candle tape (idempotent re-fetch)."""
    conn = _conn()
    conn.execute("DELETE FROM market_candles WHERE recording_id=?", (rec_id,))
    conn.executemany(
        "INSERT OR REPLACE INTO market_candles"
        " (recording_id, time, open, high, low, close, volume,"
        "  buy_volume, sell_volume, pool_sol, market_cap_usd)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (
                rec_id,
                int(c["time"]),
                float(c["open"]), float(c["high"]), float(c["low"]),
                float(c["close"]), float(c.get("volume", 0.0)),
                float(c.get("buy_volume", 0.0)),
                float(c.get("sell_volume", 0.0)),
                float(c.get("pool_sol", 0.0)),
                float(c.get("market_cap_usd", 0.0)),
            )
            for c in candles
        ],
    )
    conn.commit()
    conn.close()


def get_market_recording(market: str, symbol: str, interval: str) -> Optional[dict]:
    conn = _conn(readonly=True)
    row = conn.execute(
        "SELECT * FROM market_recordings WHERE market=? AND symbol=? AND interval=?",
        (market, symbol, interval),
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def list_market_recordings(market: Optional[str] = None) -> list[dict]:
    conn = _conn(readonly=True)
    if market:
        rows = conn.execute(
            "SELECT * FROM market_recordings WHERE market=? ORDER BY symbol",
            (market,),
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM market_recordings ORDER BY market, symbol").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_market_recording_candles(recording_id: int) -> list[dict]:
    """Candles shaped EXACTLY like data_store.get_recording_candles rows."""
    conn = _conn(readonly=True)
    rows = conn.execute(
        "SELECT time, open, high, low, close, volume, buy_volume, sell_volume,"
        " pool_sol, market_cap_usd FROM market_candles"
        " WHERE recording_id=? ORDER BY time ASC",
        (recording_id,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]
