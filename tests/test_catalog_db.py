"""Testy pro app/catalog_db.py (milestone "Data Foundation" -
persistentní SQLite katalog, zadání bod 3.1/11). Skutečná SQLite DB v
tmp_path (rychlá, izolovaná per test) - stejný vzor jako
tests/test_terrain_cache.py/test_weather.py."""

from __future__ import annotations

import pytest

from app import catalog_db


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "catalog_test.sqlite3"
    catalog_db.init_db(path)
    return path


def _row(id_="node/1", slug="test-peak", lat=49.5, lon=18.4, candidate_type="peak", **overrides):
    row = {
        "id": id_, "slug": slug, "name": "Test Peak", "display_name": "Test Peak", "name_source": "osm",
        "candidate_type": candidate_type, "lat": lat, "lon": lon, "elevation_m": 1000.0, "region": "grid_00_00",
        "municipality": None, "locality": None, "cadastral_area": None, "source": "openstreetmap", "source_id": "1",
        "near_water": None, "near_forest": None, "near_city": None,
        "nearest_water_km": None, "nearest_forest_km": None, "nearest_city_km": None,
    }
    row.update(overrides)
    return row


def test_upsert_and_get_by_id_roundtrip(db):
    catalog_db.upsert_candidate(_row(), db_path=db)
    result = catalog_db.get_by_id("node/1", db_path=db)
    assert result is not None
    assert result["display_name"] == "Test Peak"
    assert result["lat"] == 49.5


def test_get_by_slug_roundtrip(db):
    catalog_db.upsert_candidate(_row(slug="radhost"), db_path=db)
    result = catalog_db.get_by_slug("radhost", db_path=db)
    assert result["id"] == "node/1"


def test_get_missing_returns_none(db):
    assert catalog_db.get_by_id("does-not-exist", db_path=db) is None
    assert catalog_db.get_by_slug("does-not-exist", db_path=db) is None


def test_upsert_same_id_updates_not_duplicates(db):
    catalog_db.upsert_candidate(_row(display_name="First"), db_path=db)
    catalog_db.upsert_candidate(_row(display_name="Second"), db_path=db)
    assert catalog_db.count(db_path=db) == 1
    assert catalog_db.get_by_id("node/1", db_path=db)["display_name"] == "Second"


def test_upsert_many_is_one_batch(db):
    rows = [_row(id_=f"node/{i}", slug=f"peak-{i}", lat=49.5 + i * 0.01) for i in range(5)]
    catalog_db.upsert_many(rows, db_path=db)
    assert catalog_db.count(db_path=db) == 5


def test_bbox_search_finds_only_points_within_box(db):
    catalog_db.upsert_many(
        [_row(id_="node/near", slug="near", lat=50.0, lon=14.0), _row(id_="node/far", slug="far", lat=52.0, lon=20.0)],
        db_path=db,
    )
    results = catalog_db.bbox_search(49.0, 13.0, 51.0, 15.0, db_path=db)
    assert [r["id"] for r in results] == ["node/near"]


def test_bbox_search_filters_by_type(db):
    catalog_db.upsert_many(
        [
            _row(id_="node/1", slug="a", candidate_type="peak"),
            _row(id_="node/2", slug="b", candidate_type="viewpoint", lat=49.51),
        ],
        db_path=db,
    )
    results = catalog_db.bbox_search(49.0, 18.0, 50.0, 19.0, candidate_type="viewpoint", db_path=db)
    assert [r["id"] for r in results] == ["node/2"]


def test_radius_search_uses_real_haversine_distance(db):
    catalog_db.upsert_many(
        [
            _row(id_="node/near", slug="near", lat=50.0, lon=14.0),  # ~0 km od středu
            _row(id_="node/mid", slug="mid", lat=50.05, lon=14.0),  # ~5.5 km
            _row(id_="node/far", slug="far", lat=52.0, lon=14.0),  # ~222 km
        ],
        db_path=db,
    )
    results = catalog_db.radius_search(50.0, 14.0, radius_km=10.0, db_path=db)
    ids = [r["id"] for r in results]
    assert ids == ["node/near", "node/mid"]  # seřazeno podle vzdálenosti, "far" mimo radius
    assert results[0]["distance_km"] < results[1]["distance_km"]


def test_radius_search_returns_full_count_no_implicit_sql_limit(db):
    # Milestone "Correct Coverage Semantics and Candidate Accounting" -
    # /api/main.py's `found_count` je `len(catalog_db.radius_search(...))`
    # a MUSÍ to být úplný počet kandidátů v radiusu, ne jen vzorek -
    # ověřeno tady přímo proti SQL, ne jen odvozeno z názvu funkce.
    # 30 > MAX_TERRAIN_ANALYSIS_CANDIDATES (15, viz app/main.py) - pokud
    # by `radius_search()`/`bbox_search()` měly skrytý LIMIT na úrovni
    # app vrstvy, tenhle test by to odhalil.
    rows = [_row(id_=f"node/{i}", slug=f"peak-{i}", lat=50.0 + i * 0.001, lon=14.0) for i in range(30)]
    catalog_db.upsert_many(rows, db_path=db)
    results = catalog_db.radius_search(50.0, 14.0, radius_km=10.0, db_path=db)
    assert len(results) == 30


def test_radius_search_works_across_different_czech_regions():
    # Regrese na "radius search v různých částech ČR" (zadání bod 8) -
    # ověřuje, že bbox prefilter matematika funguje i mimo jeden testovací
    # bod (různé zeměpisné šířky ovlivňují stupeň->km převod na délce).
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "multi.sqlite3"
        catalog_db.init_db(db_path)
        cities = {"praha": (50.0755, 14.4378), "ostrava": (49.8209, 18.2625), "plzen": (49.7384, 13.3736)}
        rows = []
        for i, (key, (lat, lon)) in enumerate(cities.items()):
            rows.append(_row(id_=f"node/{key}", slug=key, lat=lat + 0.01, lon=lon + 0.01))
        catalog_db.upsert_many(rows, db_path=db_path)
        for key, (lat, lon) in cities.items():
            results = catalog_db.radius_search(lat, lon, radius_km=5.0, db_path=db_path)
            assert any(r["id"] == f"node/{key}" for r in results), key


def test_filter_by_type_and_region(db):
    catalog_db.upsert_many(
        [
            _row(id_="node/1", slug="a", candidate_type="castle", region="grid_00_00"),
            _row(id_="node/2", slug="b", candidate_type="peak", region="grid_01_01", lat=49.9),
        ],
        db_path=db,
    )
    assert [r["id"] for r in catalog_db.filter_by_type("castle", db_path=db)] == ["node/1"]
    assert [r["id"] for r in catalog_db.filter_by_region("grid_01_01", db_path=db)] == ["node/2"]


def test_delete_by_id_removes_candidate(db):
    catalog_db.upsert_candidate(_row(), db_path=db)
    catalog_db.delete_by_id("node/1", db_path=db)
    assert catalog_db.get_by_id("node/1", db_path=db) is None


def test_count_by_type_and_name_source(db):
    catalog_db.upsert_many(
        [
            _row(id_="node/1", slug="a", candidate_type="peak", name_source="osm"),
            _row(id_="node/2", slug="b", candidate_type="peak", name_source="generated", lat=49.6),
            _row(id_="node/3", slug="c", candidate_type="viewpoint", name_source="municipality", lat=49.7),
        ],
        db_path=db,
    )
    by_type = catalog_db.count_by_type(db_path=db)
    assert by_type == {"peak": 2, "viewpoint": 1}
    by_source = catalog_db.count_by_name_source(db_path=db)
    assert by_source == {"osm": 1, "generated": 1, "municipality": 1}


def test_environment_signal_fields_survive_roundtrip(db):
    catalog_db.upsert_candidate(
        _row(near_water=True, near_forest=False, near_city=None, nearest_water_km=1.2), db_path=db
    )
    result = catalog_db.get_by_id("node/1", db_path=db)
    assert result["near_water"] is True
    assert result["near_forest"] is False
    assert result["near_city"] is None  # nerozlišené None (nenalezeno), ne fake False
    assert result["nearest_water_km"] == 1.2


def test_list_all_returns_every_candidate(db):
    catalog_db.upsert_many([_row(id_=f"node/{i}", slug=f"s{i}", lat=49.5 + i * 0.1) for i in range(3)], db_path=db)
    assert len(catalog_db.list_all(db_path=db)) == 3
