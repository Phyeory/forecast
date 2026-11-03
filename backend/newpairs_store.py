"""
NewPairs Store: SQLite persistence for NEW-PAIR (pre-migration pump.fun)
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
feed delivers: information that no longer exists after the token dies.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from stores.recordings import RecordingStore, StoreSchema

DATA_DIR = Path(__file__).parent / "data"
DATA_DIR.mkdir(exist_ok=True)

NEWPAIRS_DB = DATA_DIR / "newpairs_data.db"

# Birth-event metadata from PumpPortal subscribeNewToken, stored alongside the
# base recording columns.
_BIRTH_COLUMNS = ("creator", "twitter", "telegram", "website",
                  "initial_sol", "market_cap_sol0", "global_fees_sol")

_NEWPAIRS_SCHEMA = StoreSchema(
    init_sql="""
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
    """,
    migrations=(
        ("ALTER TABLE recordings ADD COLUMN global_fees_sol REAL DEFAULT 0", True),
    ),
)

_STORE = RecordingStore(NEWPAIRS_DB, _NEWPAIRS_SCHEMA)


def init_newpairs_db():
    """Create the new-pair recordings schema and apply pending migrations."""
    _STORE.init_db()


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
    """Insert one new-pair recording row, with its birth metadata, and return its id."""
    return _STORE.create_recording(
        mint, timeframe, token_name, token_symbol,
        extra_columns=_BIRTH_COLUMNS,
        extra_values=(creator, twitter, telegram, website,
                      initial_sol, market_cap_sol0, global_fees_sol),
    )


def stop_recording(recording_id: int):
    """Stamp the stop time, persist the candle count, and mark the recording completed."""
    _STORE.stop_recording(recording_id)


def insert_candle(
    recording_id: int, t: int,
    o: float, h: float, l: float, c: float,
    vol: float,
    buy_vol: float = 0.0,
    sell_vol: float = 0.0,
    pool_sol: float = 0.0,
    market_cap_usd: float = 0.0,
):
    """Upsert one candle row: the previous row for the same bucket is deleted first."""
    _STORE.insert_candle(recording_id, t, o, h, l, c, vol,
                         buy_vol, sell_vol, pool_sol, market_cap_usd)


def insert_candles_batch(recording_id: int, candles: list[dict]):
    """Insert many candle rows with INSERT OR REPLACE semantics."""
    _STORE.insert_candles_batch(recording_id, candles)


def list_recordings() -> list[dict]:
    """Return every new-pair recording, newest first."""
    return _STORE.list_recordings()


def get_recording(recording_id: int) -> Optional[dict]:
    """Return one recording row, or None when the id is unknown."""
    return _STORE.get_recording(recording_id)


def get_recording_candles(recording_id: int) -> list[dict]:
    """One candle per time bucket, highest-id row per bucket."""
    return _STORE.get_recording_candles(recording_id, effective_basis=False)


def delete_recording(recording_id: int):
    """Delete one recording and its candles."""
    _STORE.delete_recording(recording_id)


def cleanup_small_recordings(min_candles: int = 30) -> int:
    """Delete recordings with fewer than ``min_candles`` candles and return the count."""
    return _STORE.cleanup_small_recordings(min_candles)


# Init on import

init_newpairs_db()
