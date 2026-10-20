"""Testy pro app/national_catalog.py (milestone "Data Foundation" -
celostátní discovery, zadání bod 3/5). Parsování testováno nad
mockovanými Overpass odpověďmi (žádná živá síť v unit testech, stejný
vzor jako tests/test_candidate_discovery.py/test_environment_context.py).
`discover_grid_cell_cached()` retry/resilience testováno přes mockovaný
`requests.post` (viz test_web.py pro stejný vzor)."""

from __future__ import annotations

import requests

from app.candidate_discovery import Candidate
from app.environment_context import EnvironmentFeatures
from app.national_catalog import (
    NationalDiscoveryError,
    PlaceRecord,
    build_candidate_rows,
    discover_grid_cell_cached,
    parse_grid_cell_response,
)


def _node(id_, lat, lon, tags):
    return {"type": "node", "id": id_, "lat": lat, "lon": lon, "tags": tags}


def _way_with_center(id_, lat, lon, tags):
    return {"type": "way", "id": id_, "center": {"lat": lat, "lon": lon}, "tags": tags}


def test_parse_extracts_all_candidate_types():
    data = {
        "elements": [
            _node(1, 49.5, 18.4, {"natural": "peak", "name": "Lysá hora", "ele": "1324"}),
            _node(2, 49.51, 18.41, {"natural": "hill", "name": "Kopec"}),
            _node(3, 49.52, 18.42, {"tourism": "viewpoint"}),
            _node(4, 49.53, 18.43, {"man_made": "tower", "tower:type": "observation", "name": "Bílá věž"}),
            _node(5, 49.54, 18.44, {"historic": "castle", "name": "Hrad"}),
            _way_with_center(6, 49.55, 18.45, {"historic": "castle", "name": "Zámek"}),
        ]
    }
    result = parse_grid_cell_response(data, "grid_00_00")
    types = sorted(c.candidate_type for c in result.candidates)
    assert types == ["castle", "castle", "hill", "peak", "tower", "viewpoint"]


def test_parse_keeps_missing_name_as_empty_not_placeholder():
    data = {"elements": [_node(1, 49.5, 18.4, {"tourism": "viewpoint"})]}
    result = parse_grid_cell_response(data, "grid_00_00")
    assert result.candidates[0].name == ""  # name resolution pipeline řeší display_name, ne parser


def test_parse_extracts_elevation_when_present():
    data = {"elements": [_node(1, 49.5, 18.4, {"natural": "peak", "ele": "1324.0"})]}
    result = parse_grid_cell_response(data, "grid_00_00")
    assert result.candidates[0].elevation_m == 1324.0


def test_parse_separates_municipality_and_locality_place_tiers():
    data = {
        "elements": [
            _node(1, 49.5, 18.4, {"place": "town", "name": "Frenštát pod Radhoštěm"}),
            _node(2, 49.51, 18.41, {"place": "city", "name": "Ostrava"}),
            _node(3, 49.52, 18.42, {"place": "hamlet", "name": "Bystré"}),
            _node(4, 49.53, 18.43, {"place": "suburb", "name": "Poruba"}),
        ]
    }
    result = parse_grid_cell_response(data, "grid_00_00")
    municipality_names = {p.name for p in result.places if p.tier == "municipality"}
    locality_names = {p.name for p in result.places if p.tier == "locality"}
    assert municipality_names == {"Frenštát pod Radhoštěm", "Ostrava"}
    assert locality_names == {"Bystré", "Poruba"}


def test_parse_skips_place_without_name():
    data = {"elements": [_node(1, 49.5, 18.4, {"place": "village"})]}
    result = parse_grid_cell_response(data, "grid_00_00")
    assert result.places == []


def test_parse_ignores_unrecognized_tags():
    data = {"elements": [_node(1, 49.5, 18.4, {"shop": "bakery"})]}
    result = parse_grid_cell_response(data, "grid_00_00")
    assert result.candidates == []
    assert result.places == []


def test_parse_skips_elements_without_coordinates():
    data = {"elements": [{"type": "way", "id": 1, "tags": {"historic": "castle"}}]}  # žádný center ani lat/lon
    result = parse_grid_cell_response(data, "grid_00_00")
    assert result.candidates == []


class _FakeResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(f"{self.status_code}")

    def json(self):
        return self._payload


def test_discover_grid_cell_cached_uses_disk_cache_without_network(monkeypatch, tmp_path):
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    (cache_dir / "grid_00_00.json").write_text('{"elements": []}')

    def fail(*a, **kw):
        raise AssertionError("nemělo dojít k síťovému požadavku - cache existuje")

    monkeypatch.setattr(requests, "post", fail)
    result = discover_grid_cell_cached("grid_00_00", (49.0, 18.0, 50.0, 19.0), cache_dir=cache_dir)
    assert result.candidates == []


def test_discover_grid_cell_cached_retries_across_endpoints_then_succeeds(monkeypatch, tmp_path):
    calls = []

    def fake_post(url, data=None, timeout=None):
        calls.append(url)
        if len(calls) < 3:
            return _FakeResponse(403, None)
        return _FakeResponse(200, {"elements": []})

    monkeypatch.setattr(requests, "post", fake_post)
    monkeypatch.setattr("time.sleep", lambda s: None)  # netestujeme reálné čekání
    result = discover_grid_cell_cached(
        "grid_00_01", (49.0, 18.0, 50.0, 19.0), cache_dir=tmp_path / "cache2",
        endpoints=("https://a.example", "https://b.example"), max_attempts_per_endpoint=2,
    )
    assert result.candidates == []
    assert len(calls) == 3  # 2 pokusy na prvním zrcadle (oba 403) + 1 úspěšný na druhém


def test_discover_grid_cell_cached_raises_after_exhausting_all_endpoints(monkeypatch, tmp_path):
    def always_fail(url, data=None, timeout=None):
        return _FakeResponse(403, None)

    monkeypatch.setattr(requests, "post", always_fail)
    monkeypatch.setattr("time.sleep", lambda s: None)
    try:
        discover_grid_cell_cached(
            "grid_00_02", (49.0, 18.0, 50.0, 19.0), cache_dir=tmp_path / "cache3",
            endpoints=("https://a.example",), max_attempts_per_endpoint=2,
        )
        assert False, "mělo vyhodit NationalDiscoveryError"
    except NationalDiscoveryError:
        pass


def _candidate(id_, name, lat, lon, candidate_type="peak", region="grid_00_00", osm_tags=None):
    return Candidate(
        id=id_, name=name, lat=lat, lon=lon, candidate_type=candidate_type, elevation_m=1000.0,
        metadata={"osm_tags": osm_tags or ({"name": name} if name else {}), "region": region},
    )


def test_build_candidate_rows_deduplicates_across_cells():
    # Stejný fyzický bod objevený DVĚMA dlaždicemi (hranice mřížky) se
    # nesmí v katalogu objevit dvakrát (zadání bod 6).
    a = _candidate("node/1", "Bod", 49.5, 18.3, region="grid_00_00")
    b = _candidate("node/2", "Bod", 49.5, 18.3, region="grid_00_01")  # jiné ID, stejná poloha
    rows, duplicates_removed = build_candidate_rows([a, b], [], {})
    assert len(rows) == 1
    assert duplicates_removed == 1


def test_build_candidate_rows_resolves_display_name_from_global_places():
    # Kandidát je v gridu "grid_00_00", ale nejbližší sídlo bylo
    # objeveno v JINÉ dlaždici (`all_places` je globální seznam přes
    # celé ČR, ne jen jedna dlaždice) - name-resolution to musí najít.
    c = _candidate("node/1", None, 49.5, 18.4, osm_tags={})
    places = [PlaceRecord(name="Frenštát pod Radhoštěm", lat=49.55, lon=18.21, tier="municipality")]
    rows, _ = build_candidate_rows([c], places, {})
    assert rows[0]["display_name"] == "Vrchol – Frenštát pod Radhoštěm"
    assert rows[0]["name_source"] == "municipality"
    assert rows[0]["name"] is None  # surové OSM name chybí - name resolution šla dál v pipeline


def test_build_candidate_rows_uses_osm_name_when_present():
    c = _candidate("node/1", "Lysá hora", 49.5, 18.4)
    rows, _ = build_candidate_rows([c], [], {})
    assert rows[0]["display_name"] == "Lysá hora"
    assert rows[0]["name_source"] == "osm"
    assert rows[0]["name"] == "Lysá hora"


def test_build_candidate_rows_applies_per_cell_environment_signal():
    c = _candidate("node/1", "X", 49.5, 18.4, region="grid_00_00")
    features = EnvironmentFeatures(water_points=[(49.5, 18.4)], forest_points=[], city_points=[])
    rows, _ = build_candidate_rows([c], [], {"grid_00_00": features})
    assert rows[0]["near_water"] is True
    assert rows[0]["near_forest"] is False


def test_build_candidate_rows_environment_none_when_cell_features_missing():
    # Dlaždice, jejíž environment fetch selhal (zadání bod 5 - "pokračuj
    # po failed requestu") - poctivě None, ne fake False.
    c = _candidate("node/1", "X", 49.5, 18.4, region="grid_99_99")
    rows, _ = build_candidate_rows([c], [], {})
    assert rows[0]["near_water"] is None
    assert rows[0]["near_forest"] is None
    assert rows[0]["near_city"] is None


def test_build_candidate_rows_filters_candidates_outside_czechia_bbox():
    paris = _candidate("node/1", "Paris", 48.8566, 2.3522)
    rows, _ = build_candidate_rows([paris], [], {})
    assert rows == []


def test_build_candidate_rows_cadastral_area_always_none_this_milestone():
    c = _candidate("node/1", "X", 49.5, 18.4)
    rows, _ = build_candidate_rows([c], [], {})
    assert rows[0]["cadastral_area"] is None


def test_discover_grid_cell_cached_does_not_write_cache_on_failure(monkeypatch, tmp_path):
    def always_fail(url, data=None, timeout=None):
        return _FakeResponse(403, None)

    monkeypatch.setattr(requests, "post", always_fail)
    monkeypatch.setattr("time.sleep", lambda s: None)
    cache_dir = tmp_path / "cache4"
    try:
        discover_grid_cell_cached(
            "grid_00_03", (49.0, 18.0, 50.0, 19.0), cache_dir=cache_dir,
            endpoints=("https://a.example",), max_attempts_per_endpoint=1,
        )
    except NationalDiscoveryError:
        pass
    assert not (cache_dir / "grid_00_03.json").exists()  # selhání se nesmí tvářit jako "hotovo, jen prázdné"
