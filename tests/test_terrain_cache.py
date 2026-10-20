"""Testy pro app/terrain_cache.py (milestone "Persistent Terrain
Cache"). Skutečná SQLite databáze v `tmp_path` (rychlá, izolovaná per
test) - žádné mockování sqlite3 samotného, protože přesně tohle je ta
věc, co potřebujeme ověřit (perzistence, status přechody, dedup)."""

from __future__ import annotations

import numpy as np
import pytest

from app.analyze_location import DemInfo, LocationInfo, StaticTerrain, TerrainInfo
from app.horizon import HorizonProfile
from app.terrain_cache import (
    STATUS_FAILED,
    STATUS_MISSING,
    STATUS_READY,
    STATUS_RUNNING,
    compute_cache_key,
    count_by_status,
    count_ready_by_region_and_type,
    get_static_terrain,
    get_status,
    init_db,
    mark_running,
    reset_stale_running,
    save_failed,
    save_ready,
)


def _fake_static(lat=49.5, lon=18.4, elevation_m=1300.0):
    azimuths = np.arange(0.0, 360.0, 1.0)
    altitudes = np.full_like(azimuths, -0.5)
    profile = HorizonProfile(azimuths_deg=azimuths, horizon_altitude_deg=altitudes)
    terrain = TerrainInfo(
        azimuths_deg=[float(a) for a in azimuths], horizon_altitudes_deg=[float(v) for v in altitudes],
        minimum_horizon_deg=-0.5, maximum_horizon_deg=-0.5, slope_deg=3.0, aspect_deg=270.0, _profile=profile,
    )
    dem = DemInfo(
        provider="cuzk_dmr5g", dataset="ČÚZK DMR 5G (DTM)", resolution_m=10.0, analysis_range_m=50000.0,
        source_crs="EPSG:4326", acquired_at=None, fallback_used=False, fallback_reason=None, quality_flag="ok",
    )
    location = LocationInfo(latitude=lat, longitude=lon, elevation_m=elevation_m)
    return StaticTerrain(location=location, dem=dem, terrain=terrain, warnings=["test warning"])


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "terrain_cache_test.sqlite3"
    init_db(path)
    return path


def test_cache_key_is_deterministic():
    assert compute_cache_key() == compute_cache_key()


def test_cache_key_changes_when_analysis_range_changes():
    assert compute_cache_key(analysis_range_m=50000.0) != compute_cache_key(analysis_range_m=30000.0)


def test_cache_key_changes_when_azimuth_step_changes():
    assert compute_cache_key(azimuth_step_deg=1.0) != compute_cache_key(azimuth_step_deg=2.0)


def test_cache_key_changes_when_observer_eye_height_changes():
    assert compute_cache_key(observer_eye_height_m=1.7) != compute_cache_key(observer_eye_height_m=2.0)


def test_cache_key_schema_version_bump_invalidates_old_key(monkeypatch):
    import app.terrain_cache as tc

    old_key = compute_cache_key()
    monkeypatch.setattr(tc, "TERRAIN_CACHE_SCHEMA_VERSION", 999)
    new_key = compute_cache_key()
    assert old_key != new_key


def test_cache_key_engine_version_bump_invalidates_old_key(monkeypatch):
    import app.terrain_cache as tc

    old_key = compute_cache_key()
    monkeypatch.setattr(tc, "TERRAIN_ENGINE_VERSION", "999")
    new_key = compute_cache_key()
    assert old_key != new_key


def test_status_missing_when_never_written(db):
    assert get_status("nope", "some-key", db_path=db) == STATUS_MISSING


def test_mark_running_sets_status(db):
    mark_running("c1", "key1", 49.5, 18.4, db_path=db)
    assert get_status("c1", "key1", db_path=db) == STATUS_RUNNING


def test_save_ready_sets_status_and_overrides_running(db):
    mark_running("c1", "key1", 49.5, 18.4, db_path=db)
    save_ready("c1", "key1", 49.5, 18.4, _fake_static(), 12.3, db_path=db)
    assert get_status("c1", "key1", db_path=db) == STATUS_READY


def test_save_failed_sets_status(db):
    save_failed("c1", "key1", 49.5, 18.4, "DEM timeout", db_path=db)
    assert get_status("c1", "key1", db_path=db) == STATUS_FAILED


def test_save_failed_after_ready_overwrites_status(db):
    # Reálný scénář: cache byla ready, ale při --force přepočtu selhala -
    # nesmí zůstat "ready" se starými daty, když nová data nejsou k dispozici.
    save_ready("c1", "key1", 49.5, 18.4, _fake_static(), 1.0, db_path=db)
    save_failed("c1", "key1", 49.5, 18.4, "nový pokus selhal", db_path=db)
    assert get_status("c1", "key1", db_path=db) == STATUS_FAILED


def test_get_static_terrain_returns_none_for_missing(db):
    assert get_static_terrain("nope", "key1", db_path=db) is None


def test_get_static_terrain_returns_none_for_running_not_ready(db):
    mark_running("c1", "key1", 49.5, 18.4, db_path=db)
    assert get_static_terrain("c1", "key1", db_path=db) is None


def test_get_static_terrain_roundtrip_preserves_values(db):
    original = _fake_static(lat=49.55, lon=18.45, elevation_m=1314.0)
    save_ready("c1", "key1", 49.55, 18.45, original, 5.0, db_path=db)
    restored = get_static_terrain("c1", "key1", db_path=db)

    assert restored is not None
    assert restored.location.latitude == original.location.latitude
    assert restored.location.longitude == original.location.longitude
    assert restored.location.elevation_m == original.location.elevation_m
    assert restored.terrain.azimuths_deg == original.terrain.azimuths_deg
    assert restored.terrain.horizon_altitudes_deg == original.terrain.horizon_altitudes_deg
    assert restored.terrain.slope_deg == original.terrain.slope_deg
    assert restored.terrain.aspect_deg == original.terrain.aspect_deg
    assert restored.dem.provider == original.dem.provider
    assert restored.warnings == original.warnings
    # HorizonProfile interpolace musí fungovat stejně jako na originále
    assert restored.terrain.horizon_at(270.0) == pytest.approx(original.terrain.horizon_at(270.0))


def test_different_cache_keys_for_same_candidate_are_independent(db):
    save_ready("c1", "key-A", 49.5, 18.4, _fake_static(elevation_m=1000.0), 1.0, db_path=db)
    assert get_status("c1", "key-A", db_path=db) == STATUS_READY
    assert get_status("c1", "key-B", db_path=db) == STATUS_MISSING  # jiný klíč = jiný záznam, i pro STEJNÉHO kandidáta


def test_persists_across_reconnect(db):
    # Simuluje restart procesu - `_connect()` se volá znovu (nová
    # instance sqlite3.Connection), soubor na disku zůstává (zadání
    # bod 17 - "cache nesmí být jen in-memory").
    save_ready("c1", "key1", 49.5, 18.4, _fake_static(), 1.0, db_path=db)
    # nové "spojení" simulované opětovným voláním veřejné funkce (interně otevře nový sqlite3.connect)
    assert get_status("c1", "key1", db_path=db) == STATUS_READY
    restored = get_static_terrain("c1", "key1", db_path=db)
    assert restored is not None


def test_count_by_status(db):
    save_ready("c1", "key1", 49.5, 18.4, _fake_static(), 1.0, db_path=db)
    save_ready("c2", "key1", 49.6, 18.5, _fake_static(), 1.0, db_path=db)
    save_failed("c3", "key1", 49.7, 18.6, "err", db_path=db)
    mark_running("c4", "key1", 49.8, 18.7, db_path=db)
    counts = count_by_status(db_path=db, cache_key="key1")
    assert counts == {"ready": 2, "failed": 1, "running": 1}


def test_count_ready_by_region_and_type(db):
    save_ready("c1", "key1", 49.5, 18.4, _fake_static(), 1.0, db_path=db, region="beskydy", candidate_type="peak")
    save_ready("c2", "key1", 49.6, 18.5, _fake_static(), 1.0, db_path=db, region="beskydy", candidate_type="viewpoint")
    save_ready("c3", "key1", 50.6, 15.5, _fake_static(), 1.0, db_path=db, region="krkonose", candidate_type="peak")
    result = count_ready_by_region_and_type(db_path=db, cache_key="key1")
    assert result["total"] == 3
    assert result["by_region"] == {"beskydy": 2, "krkonose": 1}
    assert result["by_type"] == {"peak": 2, "viewpoint": 1}


def test_count_by_status_missing_db_file_returns_zeros(tmp_path):
    result = count_by_status(db_path=tmp_path / "does_not_exist.sqlite3")
    assert result == {"missing": 0, "running": 0, "ready": 0, "failed": 0}


def test_reset_stale_running_clears_running_but_not_ready(db):
    # Zadání bod 17 - "persistent cache survival": po restartu procesu
    # žádný "running" záznam nemůže reálně dobíhat (žádný worker na
    # něj nečeká) - musí se dát znovu k dispozici pro zařazení, ne
    # zůstat navěky "zpracovává se".
    mark_running("stuck", "key1", 49.5, 18.4, db_path=db)
    save_ready("done", "key1", 49.6, 18.5, _fake_static(), 1.0, db_path=db)
    reset_stale_running(db_path=db)
    assert get_status("stuck", "key1", db_path=db) == STATUS_MISSING
    assert get_status("done", "key1", db_path=db) == STATUS_READY


def test_init_db_resets_stale_running_automatically(db):
    mark_running("stuck", "key1", 49.5, 18.4, db_path=db)
    init_db(db_path=db)  # simuluje restart procesu (main.py volá init_db() při startu)
    assert get_status("stuck", "key1", db_path=db) == STATUS_MISSING


# --- Milestone "DEM Quality and Resolution Control" - cache nesmí tiše
# smíchat/nahradit výsledek s HORŠÍM rozlišením přes ten stejný
# (candidate_id, cache_key) - `cache_key` úmyslně neobsahuje DEM
# provider/rozlišení (viz compute_cache_key() docstring), takže jediná
# ochrana je porovnat kvalitu PŘED přepsáním existujícího 'ready' řádku.


def _fake_static_with_resolution(resolution_m: float, elevation_m: float = 1300.0):
    static = _fake_static(elevation_m=elevation_m)
    static.dem.resolution_m = resolution_m
    return static


def test_save_ready_returns_true_on_first_write(db):
    assert save_ready("c1", "key1", 49.5, 18.4, _fake_static_with_resolution(10.0), 1.0, db_path=db) is True
    assert get_status("c1", "key1", db_path=db) == STATUS_READY


def test_save_ready_rejects_coarser_resolution_over_existing_ready(db):
    # Reálný scénář: 10m/px ČÚZK výsledek je 'ready'; později (např.
    # --force přepočet) ČÚZK ImageServer tiše zhrubí rozlišení na
    # 36.1m/px (nedokumentovaný limit, viz app/cuzk_dem.py) - BEZ téhle
    # ochrany by druhý zápis tiše nahradil kvalitnější data horšími.
    assert save_ready("c1", "key1", 49.5, 18.4, _fake_static_with_resolution(10.0), 1.0, db_path=db) is True
    written = save_ready("c1", "key1", 49.5, 18.4, _fake_static_with_resolution(36.1), 1.0, db_path=db)

    assert written is False
    restored = get_static_terrain("c1", "key1", db_path=db)
    assert restored.dem.resolution_m == 10.0  # původní lepší výsledek zůstal beze změny
    assert get_status("c1", "key1", db_path=db) == STATUS_READY


def test_save_ready_accepts_finer_resolution_over_existing_ready(db):
    # Opačný směr musí jít normálně - zlepšení kvality se nesmí blokovat.
    assert save_ready("c1", "key1", 49.5, 18.4, _fake_static_with_resolution(36.1), 1.0, db_path=db) is True
    written = save_ready("c1", "key1", 49.5, 18.4, _fake_static_with_resolution(10.0), 1.0, db_path=db)

    assert written is True
    restored = get_static_terrain("c1", "key1", db_path=db)
    assert restored.dem.resolution_m == 10.0


def test_save_ready_accepts_same_resolution_rewrite_over_existing_ready(db):
    # Běžný re-run se STEJNOU kvalitou (např. jen refresh computed_at)
    # se nesmí blokovat - jen jasně HORŠÍ výsledek se odmítá.
    assert save_ready("c1", "key1", 49.5, 18.4, _fake_static_with_resolution(10.0, elevation_m=1300.0), 1.0, db_path=db) is True
    written = save_ready("c1", "key1", 49.5, 18.4, _fake_static_with_resolution(10.0, elevation_m=1301.0), 2.0, db_path=db)

    assert written is True
    restored = get_static_terrain("c1", "key1", db_path=db)
    assert restored.location.elevation_m == 1301.0


def test_save_ready_coarser_resolution_allowed_when_existing_was_failed(db):
    # Horší rozlišení je stále lepší než žádný výsledek - ochrana platí
    # jen proti přepsání existujícího 'ready', ne proti prvnímu úspěchu
    # po předchozím selhání.
    save_failed("c1", "key1", 49.5, 18.4, "DEM timeout", db_path=db)
    written = save_ready("c1", "key1", 49.5, 18.4, _fake_static_with_resolution(36.1), 1.0, db_path=db)

    assert written is True
    assert get_status("c1", "key1", db_path=db) == STATUS_READY


def test_older_valid_ready_entry_remains_usable_after_rejected_rewrite(db):
    # Zadání: "zachovej všechnu existující platnou cache" - odmítnutý
    # (horší) přepočet nesmí poškodit ani jinak ovlivnit starší platný
    # záznam pro JINÉHO kandidáta se stejným cache_key.
    save_ready("other", "key1", 49.6, 18.5, _fake_static_with_resolution(10.0), 1.0, db_path=db)
    save_ready("c1", "key1", 49.5, 18.4, _fake_static_with_resolution(10.0), 1.0, db_path=db)
    save_ready("c1", "key1", 49.5, 18.4, _fake_static_with_resolution(36.1), 1.0, db_path=db)

    other = get_static_terrain("other", "key1", db_path=db)
    assert other is not None
    assert other.dem.resolution_m == 10.0
