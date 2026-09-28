"""Population-calibration determinism (2026-09-27 live-vs-BT divergence).

The live session calibrates once at open; the backtest replay of the same
recording re-runs `initialize_calibration` at the same cutoff LATER.  The two
must agree byte-exactly, but the population sample ("50 newest completed
recordings before the cutoff") used to be mutable after the fact:

  1. 1-3 s retry recordings churned the newest-50 window as they appeared.
  2. /api/recordings/cleanup deleted short recordings that earlier sessions
     had already calibrated on (Pikachu/Kabuto/Traincat/Poke diverged;
     Gacha/Nick, opened after the last cleanup, replayed byte-exact).

Membership now requires candle_count >= MIN_POPULATION_CANDLES, and the
cleanup clamps its threshold to the same value, so the sample for a fixed
cutoff is immutable.  These tests pin that contract.
"""
import sqlite3

import numpy as np
import pytest

import session_calibrator as sc


SCHEMA = """
CREATE TABLE recordings (
    id INTEGER PRIMARY KEY, mint TEXT, started_at REAL,
    stopped_at REAL, status TEXT, candle_count INTEGER);
CREATE TABLE candles (
    recording_id INTEGER, time INTEGER, open REAL, high REAL,
    low REAL, close REAL, volume REAL, buy_volume REAL,
    sell_volume REAL, pool_sol REAL);
"""


def _seed(conn, rid, start, n_candles, rng):
    """One long recording with n_candles >= MIN_POPULATION_CANDLES."""
    closes = 100.0 * np.exp(np.cumsum(rng.normal(0, 0.02, n_candles)))
    conn.execute(
        "INSERT INTO recordings VALUES (?, 'Mint', ?, ?, 'completed', ?)",
        (rid, start, start + n_candles, n_candles))
    conn.executemany(
        "INSERT INTO candles VALUES (?, ?, ?, ?, ?, ?, 1.0, 0.5, 0.5, 10.0)",
        [(rid, int(start) + i, c * 0.99, c * 1.01, c * 0.98, c)
         for i, c in enumerate(closes)])


def _seed_retry(conn, rid, start):
    """One 1-3 s retry recording — the churn/deletion hazard."""
    conn.execute(
        "INSERT INTO recordings VALUES (?, 'Mint', ?, ?, 'completed', 2)",
        (rid, start, start + 2))
    conn.executemany(
        "INSERT INTO candles VALUES (?, ?, 1.0, 1.0, 1.0, 1.0, 1.0, 0.5, 0.5, 10.0)",
        [(rid, int(start),), (rid, int(start) + 1,)])


def _build_db(tmp_path, n_long=14, retries_before_cutoff=True):
    db = tmp_path / "pop.db"
    rng = np.random.default_rng(11)
    with sqlite3.connect(db) as conn:
        conn.executescript(SCHEMA)
        rid = 1
        t = 1_000_000.0
        for _ in range(n_long):
            _seed(conn, rid, t, 140, rng)
            rid += 1
            t += 200.0
            if retries_before_cutoff and rid % 5 == 0:
                _seed_retry(conn, rid, t)
                rid += 1
                t += 3.0
        cutoff = t  # after every seeded recording
    return str(db), cutoff


def test_deleting_retry_recordings_never_changes_the_fit(tmp_path):
    """The 2026-09-27 incident: a cleanup deleted short recordings that the
    live sessions had calibrated on, shifting the population sample.  With
    membership >= MIN_POPULATION_CANDLES the fit must survive the deletion."""
    db, cutoff = _build_db(tmp_path)
    first = sc.calibrate_from_population(db, cutoff)

    with sqlite3.connect(db) as conn:
        short_ids = [r[0] for r in conn.execute(
            "SELECT id FROM recordings WHERE candle_count < ?",
            (sc.MIN_POPULATION_CANDLES,))]
        assert short_ids, "fixture must contain retry recordings"
        ph = ",".join("?" * len(short_ids))
        conn.execute(f"DELETE FROM candles WHERE recording_id IN ({ph})", short_ids)
        conn.execute(f"DELETE FROM recordings WHERE id IN ({ph})", short_ids)

    assert sc.calibrate_from_population(db, cutoff) == first


def test_retry_recordings_never_join_the_sample(tmp_path):
    """Retries must be excluded by membership, not merely filtered downstream."""
    db, cutoff = _build_db(tmp_path)
    seen = []
    original = sc._compute_rec_stats

    def spy(candles, duration):
        seen.append(int(candles[0, 0]))
        return original(candles, duration)

    sc._compute_rec_stats, bak = spy, sc._compute_rec_stats
    try:
        out = sc.calibrate_from_population(db, cutoff)
    finally:
        sc._compute_rec_stats = bak
    assert out  # population rich enough to calibrate
    with sqlite3.connect(db) as conn:
        short_ids = set(r[0] for r in conn.execute(
            "SELECT id FROM recordings WHERE candle_count < ?",
            (sc.MIN_POPULATION_CANDLES,)))
    assert short_ids
    assert not (set(seen) & short_ids)


def test_churn_after_cutoff_does_not_shift_the_window(tmp_path):
    """New short recordings created after the cutoff must not displace older
    long recordings out of the newest-50 window."""
    db, cutoff = _build_db(tmp_path, n_long=50)
    first = sc.calibrate_from_population(db, cutoff)
    with sqlite3.connect(db) as conn:
        rid = conn.execute("SELECT MAX(id) FROM recordings").fetchone()[0]
        for i in range(5):
            _seed_retry(conn, rid + 1 + i, cutoff + 10 + i * 3)
    assert sc.calibrate_from_population(db, cutoff) == first


def test_deleting_a_population_member_does_change_the_fit(tmp_path):
    """Documents the boundary the cleanup clamp enforces: a >=100-candle
    recording IS a sample member, so deleting one (manual DELETE endpoint)
    still changes the fit.  Only the membership/clamp pairing makes the
    standard cleanup path parity-safe."""
    db, cutoff = _build_db(tmp_path)
    first = sc.calibrate_from_population(db, cutoff)
    with sqlite3.connect(db) as conn:
        victim = conn.execute(
            "SELECT id FROM recordings WHERE candle_count >= ? "
            "ORDER BY started_at DESC LIMIT 1",
            (sc.MIN_POPULATION_CANDLES,)).fetchone()[0]
        conn.execute("DELETE FROM candles WHERE recording_id = ?", (victim,))
        conn.execute("DELETE FROM recordings WHERE id = ?", (victim,))
    assert sc.calibrate_from_population(db, cutoff) != first


def test_cleanup_clamp_cannot_reap_population_members(tmp_path):
    """cleanup_small_recordings clamps its threshold to MIN_POPULATION_CANDLES,
    so no requested threshold can delete a sample member."""
    import data_store
    db, cutoff = _build_db(tmp_path)
    monkey = pytest.MonkeyPatch()
    monkey.setattr(data_store, "PRICE_DB", db)
    try:
        deleted = data_store.cleanup_small_recordings(min_candles=500)
    finally:
        monkey.undo()
    assert deleted == 3  # exactly the three retry recordings
