"""Calibration caching: speed without changing live/backtest decisions."""
import asyncio
import inspect
import math
import sqlite3

import calibration_startup as startup
import session_calibrator as sc


def _recording_db(path, count=12):
    """Small completed corpus with valid OHLCV/flow/pool tapes."""
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE recordings (
            id INTEGER PRIMARY KEY, mint TEXT, started_at REAL,
            stopped_at REAL, status TEXT
        );
        CREATE TABLE candles (
            recording_id INTEGER, time INTEGER, open REAL, high REAL,
            low REAL, close REAL, volume REAL, buy_volume REAL,
            sell_volume REAL, pool_sol REAL
        );
    """)
    for i in range(count):
        started = 1_000.0 + i * 1_000.0
        stopped = started + 240.0
        conn.execute(
            "INSERT INTO recordings VALUES (?, ?, ?, ?, 'completed')",
            (i + 1, f"mint-{i}", started, stopped),
        )
        for j in range(240):
            close = 1.0 + 0.002 * j + 0.03 * math.sin((j + i) / 9.0)
            high = close * 1.01
            low = close * 0.99
            volume = 2.0 + (j % 7)
            buy = volume * (0.45 + 0.1 * math.sin(j / 5.0))
            conn.execute(
                "INSERT INTO candles VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (i + 1, int(started + j), close, high, low, close,
                 volume, buy, volume - buy, 20.0 + j * 0.01),
            )
    conn.commit()
    conn.close()


def test_population_cache_reuses_completed_recordings(monkeypatch, tmp_path):
    db = tmp_path / "population.db"
    _recording_db(db)
    sc._population_rec_cache.clear()

    calls = []
    original = sc._fetch_pool_candles

    def recording_reads(corpora, recs, before):
        calls.append([rec["id"] for rec in recs])
        return original(corpora, recs, before)

    monkeypatch.setattr(sc, "_fetch_pool_candles", recording_reads)
    kwargs = dict(n_recs=10, tau_vr=True, tau_scale=1.0, source="selected")

    cold = sc.calibrate_from_population(str(db), 10_600, **kwargs)
    assert calls and len(calls[-1]) == 10

    # Repeating a UI backtest needs no candle reads and returns exactly the
    # same coefficients (including geometry and delay knobs).
    warm = sc.calibrate_from_population(str(db), 10_600, **kwargs)
    assert warm == cold
    assert len(calls) == 1

    # Moving one session forward replaces one recording in the 50/10-rec
    # window.  The other nine are reused rather than read and recomputed.
    advanced = sc.calibrate_from_population(str(db), 11_600, **kwargs)
    assert calls[-1] == [11]

    sc._population_rec_cache.clear()
    uncached = sc.calibrate_from_population(str(db), 11_600, **kwargs)
    assert advanced == uncached


def test_population_cache_invalidates_when_history_is_rewritten(tmp_path):
    db = tmp_path / "rewritten.db"
    _recording_db(db, count=10)
    sc._population_rec_cache.clear()
    kwargs = dict(n_recs=10, source="selected")
    before = sc.calibrate_from_population(str(db), 10_600, **kwargs)

    # Historical-trade backfills are allowed to repair an existing completed
    # tape.  File versioning (including WAL) must prevent stale cached stats.
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE candles SET close=close*1.1 WHERE recording_id=1 AND time<1100")
    rewritten = sc.calibrate_from_population(str(db), 10_600, **kwargs)
    sc._population_rec_cache.clear()
    cold = sc.calibrate_from_population(str(db), 10_600, **kwargs)
    assert rewritten == cold
    assert rewritten != before


def test_population_recording_cache_is_bounded(tmp_path):
    sc._population_rec_cache.clear()
    key = (str(tmp_path), 1, 2.0)
    for i in range(sc._POPULATION_REC_CACHE_MAX + 20):
        sc._population_rec_cache_put((*key, i), {"sigma_mu": 0.1}, None)
    assert len(sc._population_rec_cache) == sc._POPULATION_REC_CACHE_MAX


def test_live_snapshot_replays_identically_with_new_explicit_knobs(
    monkeypatch, tmp_path,
):
    db = tmp_path / "live.db"
    conn = sqlite3.connect(db)
    conn.execute("""CREATE TABLE recordings (
        id INTEGER PRIMARY KEY, mint TEXT, started_at REAL,
        stopped_at REAL, status TEXT
    )""")
    conn.execute("INSERT INTO recordings VALUES (7, 'mint-live', 10000, NULL, 'recording')")
    conn.commit()
    conn.close()

    calls = []

    def fake_mint(*args):
        calls.append("mint")
        return {"sigma_mu": 0.2, "lambda_mu": 0.1,
                "gain_retrace_arm_pct": 9.0}

    def forbidden_population(*args, **kwargs):
        raise AssertionError("mint-sourced calibration must not use population")

    monkeypatch.setattr(startup, "calibrate_from_mint_history", fake_mint)
    monkeypatch.setattr(startup, "calibrate_from_population", forbidden_population)
    request = {"v2_exit_delay_seconds": 20.0, "v2_tau_vr_scale": 1.0}

    live_params, audit = asyncio.run(startup.initialize_calibration(
        "mint-live", 10_000.5, request, db_path=str(db)))
    assert calls == ["mint"]
    assert audit["source"] == "mint_history"

    # This is the main.py live-session write, performed once rec_id exists.
    from data_store import save_recording_calibration
    save_recording_calibration(
        str(db), 7, audit["cache_key"],
        audit["calibration_overrides"], audit,
    )

    expected = dict(live_params)
    live_params["sigma_mu"] = 99.0  # cached JSON must not alias the live dict
    replay_params, replay_audit = startup.initialize_calibration_sync(
        "mint-live", 10_000.5, request, db_path=str(db), recording_id=7)
    assert replay_params == expected
    assert replay_audit["cache_hit"] == "live_recording"
    assert calls == ["mint"]

    # The stored value is only the calibration layer.  A sweep's genuine
    # explicit SDE override still wins and re-derives sourcing sentinels.
    surgical, _ = startup.initialize_calibration_sync(
        "mint-live", 10_000.5, {**request, "sigma_mu": 0.07},
        db_path=str(db), recording_id=7)
    assert surgical["sigma_mu"] == 0.07
    assert surgical["lambda_mu"] == 0.1
    assert "_calibration_sourced" not in surgical

    no_harvest, _ = startup.initialize_calibration_sync(
        "mint-live", 10_000.5, {**request, "v2_harvestcal_enable": 0},
        db_path=str(db), recording_id=7)
    assert "gain_retrace_arm_pct" not in no_harvest
    assert no_harvest["v2_percoin_geo_enable"] == 0.0
    assert calls == ["mint"]


def test_backtest_is_offline_by_default_with_explicit_chain_opt_in(monkeypatch):
    monkeypatch.setattr(startup, "calibrate_from_mint_history", lambda *args: {})
    monkeypatch.setattr(
        startup, "calibrate_from_population",
        lambda *args, **kwargs: {"sigma_mu": 0.2},
    )
    calls = []

    async def fake_fetch(*args, **kwargs):
        calls.append("chain")
        return 0

    import mint_chain_history
    monkeypatch.setattr(mint_chain_history, "fetch_and_persist", fake_fetch)

    _, audit = startup.initialize_calibration_sync("unseen", 1_000)
    assert audit["chain_attempted"] is False
    assert calls == []

    _, audit = startup.initialize_calibration_sync(
        "unseen", 1_000, {"v2_chain_fetch_enable": 1})
    assert audit["chain_attempted"] is True
    assert calls == ["chain"]


def test_live_builder_persists_calibration_and_backtest_supplies_recording_id():
    import backtester
    import main
    assert "save_recording_calibration(" in inspect.getsource(
        main._get_or_create_live_session)
    source = inspect.getsource(backtester.run_backtest)
    assert "recording_id=recording_id" in source
