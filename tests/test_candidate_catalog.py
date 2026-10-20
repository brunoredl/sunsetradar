"""Testy pro app/candidate_catalog.py (milestone "Real Candidate
Catalog"). `build_catalog()` je testován nad MOCKOVANÝM
`discover_candidates_cached` (žádná síť/disk cache závislost) -
`load_candidate_catalog()` nad dočasným JSON souborem."""

from __future__ import annotations

import json

import pytest

from app.candidate_catalog import build_catalog, invalidate_catalog_cache, load_candidate_catalog
from app.candidate_discovery import Candidate
from app.environment_context import EnvironmentFeatures
from app.regions import Region


def _candidate(id_, name, lat, lon, candidate_type="peak"):
    return Candidate(id=id_, name=name, lat=lat, lon=lon, candidate_type=candidate_type, elevation_m=1000.0)


@pytest.fixture(autouse=True)
def _no_real_environment_network_calls(monkeypatch):
    """Milestone "Complete Product Build" (pokračování) - `build_catalog()`
    teď navíc volá `discover_environment_features_cached()` (reálná síť,
    Overpass) - mockováno stejně jako `discover_candidates_cached`
    (testy nezávisí na síti, viz docstring souboru). Prázdné features =
    žádný water/forest/city match - testy toho níže nezávisí."""
    monkeypatch.setattr(
        "app.candidate_catalog.discover_environment_features_cached",
        lambda region, bbox: EnvironmentFeatures(water_points=[], forest_points=[], city_points=[]),
    )


def test_build_catalog_merges_multiple_regions(monkeypatch):
    def fake_discover(region, bbox):
        if region == "a":
            return [_candidate("a1", "A1", 49.5, 18.0)]
        return [_candidate("b1", "B1", 50.0, 15.0)]

    monkeypatch.setattr("app.candidate_catalog.discover_candidates_cached", fake_discover)
    regions = {"a": Region("a", "Region A", (49.0, 17.0, 50.0, 19.0)), "b": Region("b", "Region B", (49.5, 14.0, 50.5, 16.0))}
    catalog = build_catalog(regions)
    assert catalog["candidate_count"] == 2
    ids = {c["id"] for c in catalog["candidates"]}
    assert ids == {"a1", "b1"}
    assert catalog["raw_count_per_region"] == {"a": 1, "b": 1}


def test_build_catalog_deduplicates_across_region_boundary(monkeypatch):
    # Reálný scénář (Fáze 11f): sousední regiony se dotýkají hranicí bbox -
    # stejný fyzický bod objevený OBĚMA regionálními dotazy se nesmí v
    # katalogu objevit dvakrát.
    shared_point = _candidate("shared", "Shared Peak", 49.5, 18.3)

    def fake_discover(region, bbox):
        return [shared_point]

    monkeypatch.setattr("app.candidate_catalog.discover_candidates_cached", fake_discover)
    regions = {"a": Region("a", "A", (49.0, 17.0, 50.0, 19.0)), "b": Region("b", "B", (49.0, 17.0, 50.0, 19.0))}
    catalog = build_catalog(regions)
    assert catalog["candidate_count"] == 1


def test_build_catalog_filters_candidates_outside_czechia(monkeypatch):
    outside = _candidate("paris", "Paris", 48.8566, 2.3522)  # daleko mimo CZECHIA_BBOX

    def fake_discover(region, bbox):
        return [outside]

    monkeypatch.setattr("app.candidate_catalog.discover_candidates_cached", fake_discover)
    regions = {"a": Region("a", "A", (40.0, 0.0, 50.0, 5.0))}
    catalog = build_catalog(regions)
    assert catalog["candidate_count"] == 0


def test_build_catalog_tags_each_candidate_with_its_region(monkeypatch):
    def fake_discover(region, bbox):
        return [_candidate(f"{region}-1", "X", 49.5, 18.0)]

    monkeypatch.setattr("app.candidate_catalog.discover_candidates_cached", fake_discover)
    regions = {"vsetinsko": Region("vsetinsko", "Vsetínsko", (49.0, 17.0, 50.0, 19.0))}
    catalog = build_catalog(regions)
    assert catalog["candidates"][0]["region"] == "vsetinsko"


def test_build_catalog_annotates_candidates_with_environment_signal(monkeypatch):
    def fake_discover(region, bbox):
        return [_candidate("c1", "C1", 49.5, 18.0)]

    def fake_environment(region, bbox):
        return EnvironmentFeatures(water_points=[(49.5, 18.0)], forest_points=[], city_points=[])

    monkeypatch.setattr("app.candidate_catalog.discover_candidates_cached", fake_discover)
    monkeypatch.setattr("app.candidate_catalog.discover_environment_features_cached", fake_environment)
    regions = {"a": Region("a", "A", (49.0, 17.0, 50.0, 19.0))}
    catalog = build_catalog(regions)
    c = catalog["candidates"][0]
    assert c["near_water"] is True
    assert c["near_forest"] is False
    assert c["nearest_water_km"] == 0.0


def test_load_candidate_catalog_reads_written_file(tmp_path):
    # Milestone "Data Foundation" - SQLite je teď PŘEDNOSTNÍ zdroj
    # (zadání bod 3.1), takže testy JSON fallbacku musí explicitně
    # ukázat `db_path` na neexistující soubor, jinak by se test choval
    # nedeterministicky podle toho, jestli reálný `data/national_catalog.sqlite3`
    # zrovna existuje (viz test_load_candidate_catalog_prefers_sqlite_when_present níže).
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps({"candidates": [{"id": "x", "name": "X", "lat": 1.0, "lon": 2.0}]}))
    invalidate_catalog_cache()
    result = load_candidate_catalog(path, use_cache=False, db_path=tmp_path / "no_such.sqlite3")
    assert len(result) == 1
    assert result[0]["id"] == "x"


def test_load_candidate_catalog_missing_file_returns_empty_list(tmp_path):
    invalidate_catalog_cache()
    result = load_candidate_catalog(
        tmp_path / "does_not_exist.json", use_cache=False, db_path=tmp_path / "no_such.sqlite3"
    )
    assert result == []


def test_load_candidate_catalog_caches_across_calls(tmp_path):
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps({"candidates": [{"id": "x"}]}))
    invalidate_catalog_cache()
    no_db = tmp_path / "no_such.sqlite3"
    first = load_candidate_catalog(path, use_cache=True, db_path=no_db)
    path.write_text(json.dumps({"candidates": [{"id": "x"}, {"id": "y"}]}))
    second = load_candidate_catalog(path, use_cache=True, db_path=no_db)
    assert first is second  # cache hit, soubor na disku se znovu nečetl
    invalidate_catalog_cache()
    third = load_candidate_catalog(path, use_cache=True, db_path=no_db)
    assert len(third) == 2


def test_load_candidate_catalog_prefers_sqlite_when_present(tmp_path):
    from app import catalog_db

    db_path = tmp_path / "national.sqlite3"
    catalog_db.init_db(db_path)
    catalog_db.upsert_candidate(
        {
            "id": "node/1", "slug": "test", "name": "Test", "display_name": "Test", "name_source": "osm",
            "candidate_type": "peak", "lat": 49.5, "lon": 18.4, "elevation_m": 1000.0,
        },
        db_path=db_path,
    )
    json_path = tmp_path / "legacy.json"
    json_path.write_text(json.dumps({"candidates": [{"id": "should-not-be-used"}]}))

    invalidate_catalog_cache()
    result = load_candidate_catalog(json_path, use_cache=False, db_path=db_path)
    assert len(result) == 1
    assert result[0]["id"] == "node/1"  # SQLite vyhrálo, JSON fallback se nepoužil


def test_load_candidate_catalog_falls_back_to_json_when_sqlite_empty(tmp_path):
    from app import catalog_db

    db_path = tmp_path / "empty.sqlite3"
    catalog_db.init_db(db_path)  # schéma existuje, ale žádné řádky
    json_path = tmp_path / "legacy.json"
    json_path.write_text(json.dumps({"candidates": [{"id": "legacy-1"}]}))

    invalidate_catalog_cache()
    result = load_candidate_catalog(json_path, use_cache=False, db_path=db_path)
    assert len(result) == 1
    assert result[0]["id"] == "legacy-1"
