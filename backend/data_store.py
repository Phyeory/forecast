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


def _get_price_read_conn() -> sqlite3.Connection:
    """Open a read-only connection for isolated backtest workers."""
    conn = sqlite3.connect(f"file:{PRICE_DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
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
            buy_volume   REAL DEFAULT 0,
            sell_volume  REAL DEFAULT 0,
            pool_sol     REAL DEFAULT 0,
            market_cap_usd REAL DEFAULT 0,
            FOREIGN KEY (recording_id) REFERENCES recordings(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_candles_rec_time ON candles(recording_id, time);

        CREATE TABLE IF NOT EXISTS holder_flow (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            recording_id INTEGER NOT NULL,
            time         INTEGER NOT NULL,
            wallet       TEXT NOT NULL,
            tag          TEXT DEFAULT '',
            side         TEXT NOT NULL,
            amount_usd   REAL DEFAULT 0,
            amount_sol   REAL DEFAULT 0,
            tx_hash      TEXT DEFAULT '',
            FOREIGN KEY (recording_id) REFERENCES recordings(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_holder_flow_rec_time ON holder_flow(recording_id, time);
        CREATE INDEX IF NOT EXISTS idx_holder_flow_wallet ON holder_flow(wallet);
    """)
    # Migrate existing databases that were created before buy/sell volume columns
    for col in ("buy_volume", "sell_volume", "pool_sol", "market_cap_usd"):
        try:
            conn.execute(f"ALTER TABLE candles ADD COLUMN {col} REAL DEFAULT 0")
        except Exception:
            pass  # column already exists
    # Migrate existing databases that were created before the holder_flow table
    conn.execute("""
        CREATE TABLE IF NOT EXISTS holder_flow (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            recording_id INTEGER NOT NULL,
            time         INTEGER NOT NULL,
            wallet       TEXT NOT NULL,
            tag          TEXT DEFAULT '',
            side         TEXT NOT NULL,
            amount_usd   REAL DEFAULT 0,
            amount_sol   REAL DEFAULT 0,
            tx_hash      TEXT DEFAULT '',
            FOREIGN KEY (recording_id) REFERENCES recordings(id) ON DELETE CASCADE
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_holder_flow_rec_time ON holder_flow(recording_id, time)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_holder_flow_wallet ON holder_flow(wallet)")
    conn.commit()
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


def insert_candle(
    recording_id: int, t: int,
    o: float, h: float, l: float, c: float,
    vol: float,
    buy_vol: float = 0.0,
    sell_vol: float = 0.0,
    pool_sol: float = 0.0,
    market_cap_usd: float = 0.0,
):
    """
    Upsert a single candle row.  We DELETE any existing row for the same
    (recording_id, time) bucket first so only one row ever exists per bucket.
    (The candles table has no UNIQUE constraint on those columns, so a bare
    INSERT OR REPLACE would silently insert a second row instead of replacing.)
    """
    conn = _get_price_conn()
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
    conn = _get_price_conn()
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


def insert_holder_flow(
    recording_id: int,
    t: int,
    wallet: str,
    tag: str = "",
    side: str = "sell",
    amount_usd: float = 0.0,
    amount_sol: float = 0.0,
    tx_hash: str = "",
):
    """Insert a single holder-flow event (dev/insider wallet trade)."""
    conn = _get_price_conn()
    conn.execute(
        "INSERT INTO holder_flow (recording_id, time, wallet, tag, side, amount_usd, amount_sol, tx_hash)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (recording_id, t, wallet, tag, side, amount_usd, amount_sol, tx_hash),
    )
    conn.commit()
    conn.close()


def get_holder_flow(recording_id: int, start_time: Optional[int] = None, end_time: Optional[int] = None) -> list[dict]:
    """Fetch holder-flow events for a recording, optionally filtered by time range."""
    conn = _get_price_read_conn()
    query = "SELECT * FROM holder_flow WHERE recording_id = ?"
    params: list = [recording_id]
    if start_time is not None:
        query += " AND time >= ?"
        params.append(start_time)
    if end_time is not None:
        query += " AND time <= ?"
        params.append(end_time)
    query += " ORDER BY time ASC"
    rows = conn.execute(query, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_holder_flow_since(recording_id: int, after_id: int, limit: int = 500) -> list[dict]:
    """Fetch holder-flow events with id > after_id (id-cursor incremental read).

    Used by the live session's holder-flow pump: the monitor persists every
    discovered event to this table at discovery time, so an id-cursor read
    returns exactly the events the backtester will later replay — a lossless
    delivery source that cannot drop events the way a count-diff over the
    monitor's 60s-trimmed in-memory list can.
    """
    conn = _get_price_read_conn()
    rows = conn.execute(
        "SELECT * FROM holder_flow WHERE recording_id = ? AND id > ? ORDER BY id ASC LIMIT ?",
        (recording_id, after_id, limit),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_holder_flow_for_window(recording_id: int, center_time: int, window_seconds: int) -> list[dict]:
    """Fetch holder-flow events within ±window_seconds of a center time."""
    return get_holder_flow(recording_id, center_time - window_seconds, center_time + window_seconds)


def list_recordings() -> list[dict]:
    conn = _get_price_read_conn()
    rows = conn.execute("SELECT * FROM recordings ORDER BY started_at DESC").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_recording(recording_id: int) -> Optional[dict]:
    conn = _get_price_read_conn()
    row = conn.execute("SELECT * FROM recordings WHERE id = ?", (recording_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def get_recording_candles(recording_id: int) -> list[dict]:
    """
    Return one completed candle per time bucket, ordered by time.

    Older recordings may have multiple rows per time bucket (one per tick)
    because the original recorder used INSERT OR REPLACE on a table without a
    UNIQUE constraint, silently inserting new rows on every tick instead of
    updating the existing one.  We deduplicate here by taking the row with the
    highest id (most recently written = most complete accumulated OHLCV state)
    for each time bucket.
    """
    conn = _get_price_read_conn()
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


def cleanup_small_recordings(min_candles: int = 100) -> int:
    """
    Delete recordings with fewer than `min_candles` candles (and their associated candles).
    Returns the count of deleted recordings.
    """
    conn = _get_price_conn()
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
            summary_json    TEXT DEFAULT '{}',
            batch_id        TEXT
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
            entry_params  TEXT DEFAULT '{}',
            exit_params   TEXT DEFAULT '{}',
            FOREIGN KEY (backtest_id) REFERENCES backtests(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_bt_candles ON backtest_candles(backtest_id, time);
        CREATE INDEX IF NOT EXISTS idx_bt_trades  ON backtest_trades(backtest_id);
    """)

    try:
        conn.execute("ALTER TABLE backtests ADD COLUMN batch_id TEXT;")
    except sqlite3.OperationalError:
        pass

    for col, definition in [
        ("entry_params", "TEXT DEFAULT '{}'"),
        ("exit_params",  "TEXT DEFAULT '{}'"),
    ]:
        try:
            conn.execute(f"ALTER TABLE backtest_trades ADD COLUMN {col} {definition}")
        except sqlite3.OperationalError:
            pass  # column already exists

    conn.commit()
    conn.close()


def create_backtest(recording_id: int, mint: str, token_name: str, token_symbol: str,
                    timeframe: str, engine_params: dict, stats: dict,
                    candle_results: list[dict], trades: list[dict], batch_id: Optional[str] = None) -> int:
    """Save a complete backtest run."""
    conn = _get_backtest_conn()
    cur = conn.execute(
        """INSERT INTO backtests
           (recording_id, mint, token_name, token_symbol, timeframe, engine_params,
            created_at, total_trades, win_rate, total_pnl, max_drawdown, final_balance, summary_json, batch_id)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            recording_id, mint, token_name, token_symbol, timeframe,
            json.dumps(engine_params), time.time(),
            stats.get("total_trades", 0),
            stats.get("win_rate", 0),
            stats.get("total_pnl_sol", 0),
            stats.get("max_drawdown_pct", 0),
            stats.get("current_balance", 1.0),
            json.dumps(stats),
            batch_id,
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
            size_sol, pnl_sol, pnl_pct, entry_reason, exit_reason, fees_paid, slippage_cost,
            entry_params, exit_params)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        [
            (
                bt_id, t.get("entry_time"), t.get("entry_price"),
                t.get("exit_time"), t.get("exit_price"),
                t.get("size_sol", 0), t.get("pnl_sol", 0), t.get("pnl_pct", 0),
                t.get("entry_reason", ""), t.get("exit_reason", ""),
                t.get("fees_paid", 0), t.get("slippage_cost_sol", 0),
                json.dumps(t.get("entry_params", {})),
                json.dumps(t.get("exit_params", {})),
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
    decoded_trades = []
    for t in trades:
        td = dict(t)
        td["entry_params"] = json.loads(td.get("entry_params") or "{}")
        td["exit_params"]  = json.loads(td.get("exit_params")  or "{}")
        decoded_trades.append(td)
    result["trades"] = decoded_trades
    return result


def delete_backtest(backtest_id: int):
    conn = _get_backtest_conn()
    conn.execute("DELETE FROM backtest_candles WHERE backtest_id = ?", (backtest_id,))
    conn.execute("DELETE FROM backtest_trades WHERE backtest_id = ?", (backtest_id,))
    conn.execute("DELETE FROM backtests WHERE id = ?", (backtest_id,))
    conn.commit()
    conn.close()


def delete_batch(batch_id: str):
    conn = _get_backtest_conn()
    conn.execute("DELETE FROM backtest_candles WHERE backtest_id IN (SELECT id FROM backtests WHERE batch_id = ?)", (batch_id,))
    conn.execute("DELETE FROM backtest_trades WHERE backtest_id IN (SELECT id FROM backtests WHERE batch_id = ?)", (batch_id,))
    conn.execute("DELETE FROM backtests WHERE batch_id = ?", (batch_id,))
    conn.commit()
    conn.close()


def delete_all_backtests():
    conn = _get_backtest_conn()
    conn.execute("DELETE FROM backtest_candles")
    conn.execute("DELETE FROM backtest_trades")
    conn.execute("DELETE FROM backtests")
    conn.commit()
    conn.close()


# ── Init on import ───────────────────────────────────────────────────────────

init_price_db()
init_backtest_db()
