"""Testy pro app/terrain_worker.py (milestone "Persistent Terrain
Cache + Background Precomputation"). `compute_static_terrain()` je
mockovaná (žádný reálný DEM/síť) - testy ověřují WORKER mechaniku
(fronta, dedup, concurrency, continue-after-failure), ne terrain
matematiku (ta má vlastní testy jinde)."""

from __future__ import annotations

import threading
import time

import pytest

from app import terrain_cache
from app.terrain_worker import TerrainWorker


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "worker_test.sqlite3"
    terrain_cache.init_db(path)
    return path


def _fake_static(lat, lon):
    import numpy as np

    from app.analyze_location import DemInfo, LocationInfo, StaticTerrain, TerrainInfo
    from app.horizon import HorizonProfile

    azimuths = np.arange(0.0, 360.0, 1.0)
    altitudes = np.full_like(azimuths, -0.5)
    profile = HorizonProfile(azimuths_deg=azimuths, horizon_altitude_deg=altitudes)
    terrain = TerrainInfo(
        azimuths_deg=[float(a) for a in azimuths], horizon_altitudes_deg=[float(v) for v in altitudes],
        minimum_horizon_deg=-0.5, maximum_horizon_deg=-0.5, slope_deg=1.0, aspect_deg=200.0, _profile=profile,
    )
    dem = DemInfo(
        provider="cuzk_dmr5g", dataset="ČÚZK DMR 5G (DTM)", resolution_m=10.0, analysis_range_m=50000.0,
        source_crs="EPSG:4326", acquired_at=None, fallback_used=False, fallback_reason=None, quality_flag="ok",
    )
    return StaticTerrain(location=LocationInfo(latitude=lat, longitude=lon, elevation_m=500.0), dem=dem, terrain=terrain, warnings=[])


def _wait_until(predicate, timeout_s=5.0):
    start = time.time()
    while time.time() - start < timeout_s:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_enqueue_returns_true_for_new_task(db):
    worker = TerrainWorker(db_path=db)
    assert worker.enqueue("c1", 49.5, 18.4, "key1") is True


def test_enqueue_returns_false_for_duplicate_before_processed(db):
    worker = TerrainWorker(db_path=db)
    # blokujeme worker vlákno, aby se první task nestihl zpracovat dřív, než zkusíme duplicitu
    started = threading.Event()
    release = threading.Event()

    def slow_compute(lat, lon):
        started.set()
        release.wait(timeout=2.0)
        return _fake_static(lat, lon)

    import app.terrain_worker as tw_module

    original = tw_module.compute_static_terrain
    tw_module.compute_static_terrain = slow_compute
    try:
        assert worker.enqueue("c1", 49.5, 18.4, "key1") is True
        assert started.wait(timeout=2.0)
        assert worker.enqueue("c1", 49.5, 18.4, "key1") is False  # duplicate job zamítnut (zadání bod 19)
    finally:
        release.set()
        tw_module.compute_static_terrain = original


def test_enqueue_marks_running_immediately(db):
    worker = TerrainWorker(db_path=db)

    def slow_compute(lat, lon):
        time.sleep(0.3)
        return _fake_static(lat, lon)

    import app.terrain_worker as tw_module

    tw_module.compute_static_terrain = slow_compute
    try:
        worker.enqueue("c1", 49.5, 18.4, "key1")
        assert _wait_until(lambda: terrain_cache.get_status("c1", "key1", db_path=db) == terrain_cache.STATUS_RUNNING, timeout_s=1.0)
    finally:
        tw_module.compute_static_terrain = lambda lat, lon: _fake_static(lat, lon)


def test_successful_task_saves_ready_status(db, monkeypatch):
    import app.terrain_worker as tw_module

    monkeypatch.setattr(tw_module, "compute_static_terrain", lambda lat, lon: _fake_static(lat, lon))
    worker = TerrainWorker(db_path=db)
    worker.enqueue("c1", 49.5, 18.4, "key1", region="beskydy", candidate_type="peak")
    assert _wait_until(lambda: terrain_cache.get_status("c1", "key1", db_path=db) == terrain_cache.STATUS_READY)
    static = terrain_cache.get_static_terrain("c1", "key1", db_path=db)
    assert static is not None
    assert static.location.latitude == 49.5


def test_degraded_resolution_result_is_saved_as_ready_not_failed(db, monkeypatch):
    # Milestone "DEM Quality and Resolution Control" - zhrubené rozlišení
    # (ČÚZK ImageServer limit) NENÍ chyba/nekompletní výsledek - pokud
    # compute_static_terrain() nevyhodí výjimku, je to platný (jen horší
    # kvality) výsledek a správně se uloží jako 'ready', ne 'failed'.
    import app.terrain_worker as tw_module

    def degraded_compute(lat, lon):
        static = _fake_static(lat, lon)
        static.dem.resolution_m = 36.1
        return static

    monkeypatch.setattr(tw_module, "compute_static_terrain", degraded_compute)
    worker = TerrainWorker(db_path=db)
    worker.enqueue("c1", 49.5, 18.4, "key1")
    assert _wait_until(lambda: terrain_cache.get_status("c1", "key1", db_path=db) == terrain_cache.STATUS_READY)
    static = terrain_cache.get_static_terrain("c1", "key1", db_path=db)
    assert static.dem.resolution_m == 36.1


def test_worker_rejected_rewrite_leaves_existing_ready_status_intact(db, monkeypatch):
    # Worker nesmí spadnout/zůstat v nekonzistentním stavu, pokud
    # terrain_cache.save_ready() odmítne zápis (horší kvalita přes
    # existující lepší 'ready' záznam) - status musí zůstat 'ready'.
    import app.terrain_worker as tw_module

    monkeypatch.setattr(tw_module, "compute_static_terrain", lambda lat, lon: _fake_static(lat, lon))
    worker = TerrainWorker(db_path=db)
    worker.enqueue("c1", 49.5, 18.4, "key1")
    assert _wait_until(lambda: terrain_cache.get_status("c1", "key1", db_path=db) == terrain_cache.STATUS_READY)
    good_static = terrain_cache.get_static_terrain("c1", "key1", db_path=db)

    def degraded_compute(lat, lon):
        static = _fake_static(lat, lon)
        static.dem.resolution_m = 99.0
        return static

    monkeypatch.setattr(tw_module, "compute_static_terrain", degraded_compute)
    worker.enqueue("c1", 49.5, 18.4, "key1")
    time.sleep(0.2)  # worker zpracuje task; žádný status-flip, žádná výjimka se neočekává
    assert terrain_cache.get_status("c1", "key1", db_path=db) == terrain_cache.STATUS_READY
    restored = terrain_cache.get_static_terrain("c1", "key1", db_path=db)
    assert restored.dem.resolution_m == good_static.dem.resolution_m == 10.0


def test_failed_task_saves_failed_status_and_worker_continues(db, monkeypatch):
    import app.terrain_worker as tw_module

    calls = []

    def flaky(lat, lon):
        calls.append((lat, lon))
        if lat == 1.0:
            raise RuntimeError("DEM timeout")
        return _fake_static(lat, lon)

    monkeypatch.setattr(tw_module, "compute_static_terrain", flaky)
    worker = TerrainWorker(db_path=db)
    worker.enqueue("bad", 1.0, 1.0, "key1")
    assert _wait_until(lambda: terrain_cache.get_status("bad", "key1", db_path=db) == terrain_cache.STATUS_FAILED)

    # worker musí pokračovat na DALŠÍM úkolu i po selhání předchozího (zadání bod 9)
    worker.enqueue("good", 49.5, 18.4, "key1")
    assert _wait_until(lambda: terrain_cache.get_status("good", "key1", db_path=db) == terrain_cache.STATUS_READY)


def test_concurrency_limit_respected(db, monkeypatch):
    import app.terrain_worker as tw_module

    concurrent_count = {"current": 0, "max_seen": 0}
    lock = threading.Lock()

    def tracking_compute(lat, lon):
        with lock:
            concurrent_count["current"] += 1
            concurrent_count["max_seen"] = max(concurrent_count["max_seen"], concurrent_count["current"])
        time.sleep(0.1)
        with lock:
            concurrent_count["current"] -= 1
        return _fake_static(lat, lon)

    monkeypatch.setattr(tw_module, "compute_static_terrain", tracking_compute)
    worker = TerrainWorker(concurrency=1, db_path=db)
    for i in range(4):
        worker.enqueue(f"c{i}", 49.5 + i * 0.01, 18.4, "key1")

    assert _wait_until(lambda: all(terrain_cache.get_status(f"c{i}", "key1", db_path=db) == terrain_cache.STATUS_READY for i in range(4)), timeout_s=5.0)
    assert concurrent_count["max_seen"] == 1  # nikdy víc než TERRAIN_WORKER_CONCURRENCY souběžně (zadání bod 9)


def test_is_pending_reflects_queue_state(db, monkeypatch):
    import app.terrain_worker as tw_module

    started = threading.Event()
    release = threading.Event()

    def slow(lat, lon):
        started.set()
        release.wait(timeout=2.0)
        return _fake_static(lat, lon)

    monkeypatch.setattr(tw_module, "compute_static_terrain", slow)
    worker = TerrainWorker(db_path=db)
    assert worker.is_pending("c1", "key1") is False
    worker.enqueue("c1", 49.5, 18.4, "key1")
    assert started.wait(timeout=2.0)
    assert worker.is_pending("c1", "key1") is True
    release.set()
    assert _wait_until(lambda: not worker.is_pending("c1", "key1"))
