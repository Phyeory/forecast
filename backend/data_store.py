"""
Data Store — SQLite persistence for price recordings and backtest results.

Two databases:
  1. price_data.db   — Raw OHLCV candle recordings
  2. backtest_data.db — Backtest results (candles + signals + regimes + trades)
"""

from __future__ import annotations
import json
import sqlite3
import time
from pathlib import Path
from typing import Optional

DATA_DIR = Path(__file__).parent / "data"
DATA_DIR.mkdir(exist_ok=True)

PRICE_DB = DATA_DIR / "price_data.db"
BACKTEST_DB = DATA_DIR / "backtest_data.db"


# ── Price Recording DB ───────────────────────────────────────────────────────

def _get_price_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(PRICE_DB))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_price_db():
    conn = _get_price_conn()
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
            status      TEXT DEFAULT 'recording'
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
            FOREIGN KEY (recording_id) REFERENCES recordings(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_candles_rec_time ON candles(recording_id, time);
    """)
    conn.close()


def create_recording(mint: str, timeframe: str, token_name: str = "", token_symbol: str = "") -> int:
    conn = _get_price_conn()
    cur = conn.execute(
        "INSERT INTO recordings (mint, token_name, token_symbol, timeframe, started_at) VALUES (?, ?, ?, ?, ?)",
        (mint, token_name, token_symbol, timeframe, time.time()),
    )
    rec_id = cur.lastrowid
    conn.commit()
    conn.close()
    return rec_id


def stop_recording(recording_id: int):
    conn = _get_price_conn()
    count = conn.execute("SELECT COUNT(*) FROM candles WHERE recording_id = ?", (recording_id,)).fetchone()[0]
    conn.execute(
        "UPDATE recordings SET stopped_at = ?, candle_count = ?, status = 'completed' WHERE id = ?",
        (time.time(), count, recording_id),
    )
    conn.commit()
    conn.close()


def insert_candle(recording_id: int, t: int, o: float, h: float, l: float, c: float, vol: float):
    conn = _get_price_conn()
    conn.execute(
        "INSERT OR REPLACE INTO candles (recording_id, time, open, high, low, close, volume) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (recording_id, t, o, h, l, c, vol),
    )
    conn.commit()
    conn.close()


def insert_candles_batch(recording_id: int, candles: list[dict]):
    conn = _get_price_conn()
    conn.executemany(
        "INSERT OR REPLACE INTO candles (recording_id, time, open, high, low, close, volume) VALUES (?, ?, ?, ?, ?, ?, ?)",
        [(recording_id, c["time"], c["open"], c["high"], c["low"], c["close"], c.get("volume", 0)) for c in candles],
    )
    conn.commit()
    conn.close()


def list_recordings() -> list[dict]:
    conn = _get_price_conn()
    rows = conn.execute("SELECT * FROM recordings ORDER BY started_at DESC").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_recording(recording_id: int) -> Optional[dict]:
    conn = _get_price_conn()
    row = conn.execute("SELECT * FROM recordings WHERE id = ?", (recording_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def get_recording_candles(recording_id: int) -> list[dict]:
    conn = _get_price_conn()
    rows = conn.execute(
        "SELECT time, open, high, low, close, volume FROM candles WHERE recording_id = ? ORDER BY time",
        (recording_id,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def delete_recording(recording_id: int):
    conn = _get_price_conn()
    conn.execute("DELETE FROM candles WHERE recording_id = ?", (recording_id,))
    conn.execute("DELETE FROM recordings WHERE id = ?", (recording_id,))
    conn.commit()
    conn.close()


def update_recording_candle_count(recording_id: int):
    conn = _get_price_conn()
    count = conn.execute("SELECT COUNT(*) FROM candles WHERE recording_id = ?", (recording_id,)).fetchone()[0]
    conn.execute("UPDATE recordings SET candle_count = ? WHERE id = ?", (count, recording_id))
    conn.commit()
    conn.close()


# ── Backtest DB ──────────────────────────────────────────────────────────────

def _get_backtest_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(BACKTEST_DB))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_backtest_db():
    conn = _get_backtest_conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS backtests (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            recording_id    INTEGER NOT NULL,
            mint            TEXT NOT NULL,
            token_name      TEXT DEFAULT '',
            token_symbol    TEXT DEFAULT '',
            timeframe       TEXT NOT NULL,
            engine_params   TEXT DEFAULT '{}',
            created_at      REAL NOT NULL,
            total_trades    INTEGER DEFAULT 0,
            win_rate        REAL DEFAULT 0,
            total_pnl       REAL DEFAULT 0,
            max_drawdown    REAL DEFAULT 0,
            final_balance   REAL DEFAULT 1.0,
            summary_json    TEXT DEFAULT '{}'
        );

        CREATE TABLE IF NOT EXISTS backtest_candles (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            backtest_id   INTEGER NOT NULL,
            time          INTEGER NOT NULL,
            open          REAL NOT NULL,
            high          REAL NOT NULL,
            low           REAL NOT NULL,
            close         REAL NOT NULL,
            volume        REAL DEFAULT 0,
            regime        TEXT DEFAULT 'idle',
            direction     TEXT DEFAULT 'none',
            signal        TEXT DEFAULT 'none',
            signal_strength REAL DEFAULT 0,
            ema_fast      REAL,
            ema_slow      REAL,
            atr           REAL,
            roc           REAL,
            confidence    REAL DEFAULT 0,
            trade_action  TEXT,
            trade_label   TEXT,
            balance       REAL,
            unrealized_pnl REAL DEFAULT 0,
            FOREIGN KEY (backtest_id) REFERENCES backtests(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS backtest_trades (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            backtest_id   INTEGER NOT NULL,
            entry_time    INTEGER,
            entry_price   REAL,
            exit_time     INTEGER,
            exit_price    REAL,
            size_sol      REAL,
            pnl_sol       REAL DEFAULT 0,
            pnl_pct       REAL DEFAULT 0,
            entry_reason  TEXT DEFAULT '',
            exit_reason   TEXT DEFAULT '',
            fees_paid     REAL DEFAULT 0,
            slippage_cost REAL DEFAULT 0,
            FOREIGN KEY (backtest_id) REFERENCES backtests(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_bt_candles ON backtest_candles(backtest_id, time);
        CREATE INDEX IF NOT EXISTS idx_bt_trades  ON backtest_trades(backtest_id);
    """)
    conn.close()


def create_backtest(recording_id: int, mint: str, token_name: str, token_symbol: str,
                    timeframe: str, engine_params: dict, stats: dict,
                    candle_results: list[dict], trades: list[dict]) -> int:
    """Save a complete backtest run."""
    conn = _get_backtest_conn()
    cur = conn.execute(
        """INSERT INTO backtests
           (recording_id, mint, token_name, token_symbol, timeframe, engine_params,
            created_at, total_trades, win_rate, total_pnl, max_drawdown, final_balance, summary_json)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            recording_id, mint, token_name, token_symbol, timeframe,
            json.dumps(engine_params), time.time(),
            stats.get("total_trades", 0),
            stats.get("win_rate", 0),
            stats.get("total_pnl_sol", 0),
            stats.get("max_drawdown_pct", 0),
            stats.get("current_balance", 1.0),
            json.dumps(stats),
        ),
    )
    bt_id = cur.lastrowid

    # Insert candle results
    conn.executemany(
        """INSERT INTO backtest_candles
           (backtest_id, time, open, high, low, close, volume,
            regime, direction, signal, signal_strength,
            ema_fast, ema_slow, atr, roc, confidence,
            trade_action, trade_label, balance, unrealized_pnl)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        [
            (
                bt_id, r["time"], r["open"], r["high"], r["low"], r["close"], r.get("volume", 0),
                r.get("regime", "idle"), r.get("direction", "none"), r.get("signal", "none"),
                r.get("signal_strength", 0),
                r.get("ema_fast"), r.get("ema_slow"), r.get("atr"), r.get("roc"),
                r.get("confidence", 0),
                r.get("trade_action"), r.get("trade_label"),
                r.get("balance"), r.get("unrealized_pnl", 0),
            )
            for r in candle_results
        ],
    )

    # Insert trades
    conn.executemany(
        """INSERT INTO backtest_trades
           (backtest_id, entry_time, entry_price, exit_time, exit_price,
            size_sol, pnl_sol, pnl_pct, entry_reason, exit_reason, fees_paid, slippage_cost)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        [
            (
                bt_id, t.get("entry_time"), t.get("entry_price"),
                t.get("exit_time"), t.get("exit_price"),
                t.get("size_sol", 0), t.get("pnl_sol", 0), t.get("pnl_pct", 0),
                t.get("entry_reason", ""), t.get("exit_reason", ""),
                t.get("fees_paid", 0), t.get("slippage_cost_sol", 0),
            )
            for t in trades
        ],
    )

    conn.commit()
    conn.close()
    return bt_id


def list_backtests() -> list[dict]:
    conn = _get_backtest_conn()
    rows = conn.execute("SELECT * FROM backtests ORDER BY created_at DESC").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_backtest(backtest_id: int) -> Optional[dict]:
    conn = _get_backtest_conn()
    bt = conn.execute("SELECT * FROM backtests WHERE id = ?", (backtest_id,)).fetchone()
    if not bt:
        conn.close()
        return None

    candles = conn.execute(
        "SELECT * FROM backtest_candles WHERE backtest_id = ? ORDER BY time",
        (backtest_id,),
    ).fetchall()

    trades = conn.execute(
        "SELECT * FROM backtest_trades WHERE backtest_id = ? ORDER BY entry_time",
        (backtest_id,),
    ).fetchall()

    conn.close()

    result = dict(bt)
    result["engine_params"] = json.loads(result.get("engine_params", "{}"))
    result["summary_json"] = json.loads(result.get("summary_json", "{}"))
    result["candles"] = [dict(c) for c in candles]
    result["trades"] = [dict(t) for t in trades]
    return result


def delete_backtest(backtest_id: int):
    conn = _get_backtest_conn()
    conn.execute("DELETE FROM backtest_candles WHERE backtest_id = ?", (backtest_id,))
    conn.execute("DELETE FROM backtest_trades WHERE backtest_id = ?", (backtest_id,))
    conn.execute("DELETE FROM backtests WHERE id = ?", (backtest_id,))
    conn.commit()
    conn.close()


# ── Init on import ───────────────────────────────────────────────────────────

init_price_db()
init_backtest_db()
