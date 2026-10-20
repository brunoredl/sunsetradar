"""Testy pro app/candidate_discovery.py (Fáze 8). Parsování/filtrace
testováno čistě nad mockovanými Overpass odpověďmi - ŽÁDNÁ živá síť
(mimo jediný @pytest.mark.integration test na konci, stejný vzor jako
tests/test_web.py pro DEM zdroje)."""

from __future__ import annotations

import pytest
import requests

from app.candidate_discovery import (
    Candidate,
    CandidateDiscoveryError,
    deduplicate,
    discover_candidates,
    discover_candidates_cached,
    filter_within_bbox,
    haversine_m,
    parse_overpass_response,
)


def _node(id_, lat, lon, tags):
    return {"type": "node", "id": id_, "lat": lat, "lon": lon, "tags": tags}


def test_parse_extracts_peak_hill_viewpoint():
    data = {
        "elements": [
            _node(1, 49.5459784, 18.4472696, {"natural": "peak", "name": "Lysá hora", "ele": "1324"}),
            _node(2, 49.5846448, 18.3129392, {"natural": "hill", "name": "Ondřejník", "ele": "890"}),
            _node(3, 49.5808539, 18.3198198, {"tourism": "viewpoint", "name": "Solárka"}),
        ]
    }
    candidates = parse_overpass_response(data)
    assert len(candidates) == 3
    by_name = {c.name: c for c in candidates}
    assert by_name["Lysá hora"].candidate_type == "peak"
    assert by_name["Lysá hora"].elevation_m == 1324.0
    assert by_name["Ondřejník"].candidate_type == "hill"
    assert by_name["Solárka"].candidate_type == "viewpoint"
    assert by_name["Solárka"].elevation_m is None


def test_parse_extracts_observation_tower_as_own_type():
    # Fáze 11 audit - rozhledna potřebuje OBA tagy `man_made=tower` +
    # `tower:type=observation`; samotné `man_made=tower` (vysílač,
    # vodojem...) se nesmí zachytit.
    data = {
        "elements": [
            _node(1, 49.6, 18.3, {"man_made": "tower", "tower:type": "observation", "name": "Prašivá"}),
            _node(2, 49.6, 18.3, {"man_made": "tower", "name": "Vysílač (bez tower:type)"}),
        ]
    }
    candidates = parse_overpass_response(data)
    assert len(candidates) == 1
    assert candidates[0].name == "Prašivá"
    assert candidates[0].candidate_type == "tower"


def test_parse_tower_with_viewpoint_tag_classifies_as_single_tower_candidate():
    # Hodně rozhleden má ZÁROVEŇ `tourism=viewpoint` (ověřeno auditem) -
    # nesmí vzniknout dva kandidáti pro stejný OSM uzel, a výsledný typ
    # musí být "tower" (specifičtější), ne obyčejná "viewpoint".
    data = {
        "elements": [
            _node(1, 49.6, 18.3, {"man_made": "tower", "tower:type": "observation", "tourism": "viewpoint", "name": "Rozhledna"}),
        ]
    }
    candidates = parse_overpass_response(data)
    assert len(candidates) == 1
    assert candidates[0].candidate_type == "tower"


def test_parse_skips_way_and_relation_elements():
    data = {
        "elements": [
            {"type": "way", "id": 10, "center": {"lat": 49.5, "lon": 18.4}, "tags": {"natural": "peak", "name": "X"}},
            {"type": "relation", "id": 11, "tags": {"natural": "peak", "name": "Y"}},
            _node(12, 49.5, 18.4, {"natural": "peak", "name": "Skutečný vrchol"}),
        ]
    }
    candidates = parse_overpass_response(data)
    assert len(candidates) == 1
    assert candidates[0].name == "Skutečný vrchol"


def test_parse_falls_back_to_generated_name_when_missing():
    data = {"elements": [_node(1, 49.5, 18.4, {"tourism": "viewpoint"})]}
    candidates = parse_overpass_response(data)
    assert len(candidates) == 1
    assert "49.500" in candidates[0].name
    assert "18.400" in candidates[0].name


def test_parse_skips_unrecognized_tags():
    data = {
        "elements": [
            _node(1, 49.5, 18.4, {"amenity": "restaurant", "name": "Restaurace U Lva"}),
            _node(2, 49.5, 18.4, {"shop": "bakery", "name": "Pekárna"}),
        ]
    }
    assert parse_overpass_response(data) == []


def test_parse_skips_missing_coordinates():
    data = {"elements": [{"type": "node", "id": 1, "tags": {"natural": "peak", "name": "Bez souřadnic"}}]}
    assert parse_overpass_response(data) == []


def test_parse_handles_invalid_elevation_gracefully():
    data = {"elements": [_node(1, 49.5, 18.4, {"natural": "peak", "name": "X", "ele": "not-a-number"})]}
    candidates = parse_overpass_response(data)
    assert candidates[0].elevation_m is None


def test_parse_empty_candidate_set():
    assert parse_overpass_response({"elements": []}) == []
    assert parse_overpass_response({}) == []


def test_deduplicate_removes_close_points():
    a = Candidate(id="a", name="A", lat=49.5, lon=18.4, candidate_type="peak", elevation_m=1000)
    b = Candidate(id="b", name="B", lat=49.50001, lon=18.40001, candidate_type="peak", elevation_m=999)  # ~1.3 m od A
    result = deduplicate([a, b], min_distance_m=150.0)
    assert result == [a]


def test_deduplicate_keeps_far_points():
    a = Candidate(id="a", name="A", lat=49.5, lon=18.4, candidate_type="peak", elevation_m=1000)
    b = Candidate(id="b", name="B", lat=49.6, lon=18.5, candidate_type="peak", elevation_m=999)
    result = deduplicate([a, b], min_distance_m=150.0)
    assert result == [a, b]


# --- Fáze 11 - haversine_m (zadání bod V - "distance: správná
# vzdálenost, stabilní výsledek"; sdílí ji i /api/search v app/main.py) ---


def test_haversine_m_identical_points_is_zero():
    assert haversine_m(49.5, 18.4, 49.5, 18.4) == 0.0


def test_haversine_m_matches_known_reference_distance():
    # 1 stupeň zeměpisné šířky ≈ 111.32 km (standardní geodetická
    # konstanta) - podél poledníku na vzdálenosti nezáleží zeměpisná délka.
    dist_km = haversine_m(50.0, 14.0, 51.0, 14.0) / 1000.0
    assert dist_km == pytest.approx(111.19, abs=0.5)


def test_haversine_m_is_stable_across_repeated_calls():
    results = {haversine_m(49.5459784, 18.4472696, 50.7365, 15.7404) for _ in range(5)}
    assert len(results) == 1


def test_deduplicate_boundary_exactly_at_min_distance_is_kept():
    # `deduplicate()` odstraňuje jen body PŘÍSNĚ BLÍŽ než `min_distance_m`
    # (`< min_distance_m`, viz app/candidate_discovery.py) - bod přesně
    # na hranici zůstává.
    a = Candidate(id="a", name="A", lat=49.5, lon=18.4, candidate_type="peak", elevation_m=1000)
    b = Candidate(id="b", name="B", lat=49.5013, lon=18.4, candidate_type="peak", elevation_m=999)
    # `min_distance_m` nastavené na PŘESNĚ naměřenou vzdálenost mezi a/b -
    # `deduplicate()` odstraňuje jen bod PŘÍSNĚ blíž (`< min_distance_m`,
    # viz app/candidate_discovery.py), takže bod přesně na hranici musí
    # zůstat (porovnání stejné hodnoty samo se sebou nikdy není `<`,
    # žádná nejistota z zaokrouhlení plovoucí desetinné čárky).
    exact_dist_m = haversine_m(a.lat, a.lon, b.lat, b.lon)
    result = deduplicate([a, b], min_distance_m=exact_dist_m)
    assert result == [a, b]


def test_filter_within_bbox_keeps_inside_points():
    inside = Candidate(id="a", name="A", lat=49.5, lon=18.4, candidate_type="peak", elevation_m=1000)
    outside = Candidate(id="b", name="B", lat=60.0, lon=18.4, candidate_type="peak", elevation_m=1000)  # Skandinávie
    result = filter_within_bbox([inside, outside], bbox=(48.5, 12.0, 51.1, 19.0))
    assert result == [inside]


def test_discover_candidates_raises_clear_error_on_network_failure(monkeypatch):
    class FakeSession:
        def post(self, *args, **kwargs):
            raise requests.exceptions.ConnectionError("network down")

    with pytest.raises(CandidateDiscoveryError):
        discover_candidates((49.4, 18.3, 49.7, 18.7), session=FakeSession())


def test_discover_candidates_parses_successful_response(monkeypatch):
    class FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {"elements": [_node(1, 49.5, 18.4, {"natural": "peak", "name": "Test Peak", "ele": "1000"})]}

    class FakeSession:
        def post(self, url, data=None, timeout=None):
            assert "data" in data
            return FakeResponse()

    result = discover_candidates((49.4, 18.3, 49.7, 18.7), session=FakeSession())
    assert len(result) == 1
    assert result[0].name == "Test Peak"


def test_discover_candidates_cached_uses_existing_cache_without_network(tmp_path, monkeypatch):
    cache_dir = tmp_path
    (cache_dir / "testregion_combined.json").write_text(
        '{"elements": [{"type": "node", "id": 1, "lat": 49.5, "lon": 18.4, "tags": {"natural": "peak", "name": "Z cache"}}]}'
    )

    def fail_if_called(*args, **kwargs):
        raise AssertionError("nemělo dojít k síťovému požadavku, když cache existuje")

    monkeypatch.setattr("app.candidate_discovery.requests.post", fail_if_called)
    result = discover_candidates_cached("testregion", (49.4, 18.3, 49.7, 18.7), cache_dir=cache_dir)
    assert len(result) == 1
    assert result[0].name == "Z cache"


def test_discover_candidates_cached_writes_cache_on_miss(tmp_path, monkeypatch):
    class FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {"elements": [{"type": "node", "id": 2, "lat": 49.5, "lon": 18.4, "tags": {"natural": "hill", "name": "Nový"}}]}

    monkeypatch.setattr("app.candidate_discovery.requests.post", lambda *a, **k: FakeResponse())
    result = discover_candidates_cached("newregion", (49.4, 18.3, 49.7, 18.7), cache_dir=tmp_path)
    assert len(result) == 1
    assert (tmp_path / "newregion_combined.json").exists()


@pytest.mark.integration
def test_real_overpass_query_for_beskydy_returns_known_peaks():
    """Skutečný síťový dotaz (stejný vzor jako ostatní @integration testy
    v projektu) - ověřuje, že Overpass zrcadlo je dostupné a vrací
    rozpoznatelná reálná data (ne jen strukturálně validní odpověď)."""
    candidates = discover_candidates((49.4, 18.3, 49.7, 18.7))
    names = {c.name for c in candidates}
    assert "Lysá hora" in names  # nejvyšší vrchol Moravskoslezských Beskyd - musí tam být
    assert len(candidates) > 20
