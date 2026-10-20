"""Testy pro app/environment_context.py (milestone "Complete Product
Build" - reálný Water/Forest/City signál). Parsování testováno nad
mockovanými Overpass odpověďmi - žádná živá síť v unit testech (stejný
vzor jako tests/test_candidate_discovery.py)."""

from __future__ import annotations

from app.environment_context import (
    NEAR_CITY_KM,
    NEAR_FOREST_KM,
    NEAR_WATER_KM,
    classify_candidate_environment,
    parse_environment_response,
)


def _node(id_, lat, lon, tags):
    return {"type": "node", "id": id_, "lat": lat, "lon": lon, "tags": tags}


def _way_with_center(id_, lat, lon, tags):
    return {"type": "way", "id": id_, "center": {"lat": lat, "lon": lon}, "tags": tags}


def test_parse_extracts_water_forest_city():
    data = {
        "elements": [
            _node(1, 49.5, 18.4, {"natural": "water", "name": "Rybník"}),
            _way_with_center(2, 49.51, 18.41, {"natural": "water"}),
            _way_with_center(3, 49.52, 18.42, {"waterway": "river", "name": "Ostravice"}),
            _way_with_center(4, 49.53, 18.43, {"natural": "wood"}),
            _way_with_center(5, 49.54, 18.44, {"landuse": "forest"}),
            _node(6, 49.55, 18.45, {"place": "town", "name": "Frýdek-Místek"}),
            _node(7, 49.56, 18.46, {"place": "city", "name": "Ostrava"}),
        ]
    }
    features = parse_environment_response(data)
    assert len(features.water_points) == 3
    assert len(features.forest_points) == 2
    assert len(features.city_points) == 2


def test_parse_skips_unrecognized_tags():
    data = {"elements": [_node(1, 49.5, 18.4, {"shop": "bakery"})]}
    features = parse_environment_response(data)
    assert features.water_points == []
    assert features.forest_points == []
    assert features.city_points == []


def test_parse_skips_elements_without_coordinates():
    data = {"elements": [{"type": "way", "id": 1, "tags": {"natural": "water"}}]}  # žádný center ani lat/lon
    features = parse_environment_response(data)
    assert features.water_points == []


def test_parse_village_is_not_counted_as_city():
    # zadání rozlišuje "city/town" (dost velké na "City" preferenci) od
    # malých vesnic - "village"/"hamlet" by dávaly falešně pozitivní match.
    data = {"elements": [_node(1, 49.5, 18.4, {"place": "village", "name": "Malá Ves"})]}
    features = parse_environment_response(data)
    assert features.city_points == []


def _features(water=None, forest=None, city=None):
    from app.environment_context import EnvironmentFeatures

    return EnvironmentFeatures(water_points=water or [], forest_points=forest or [], city_points=city or [])


def test_classify_near_water_within_threshold():
    features = _features(water=[(49.5, 18.4)])
    # posun o cca 1 km severně
    result = classify_candidate_environment(49.509, 18.4, features)
    assert result.near_water is True
    assert result.nearest_water_km is not None
    assert result.nearest_water_km < NEAR_WATER_KM


def test_classify_not_near_water_beyond_threshold():
    features = _features(water=[(49.5, 18.4)])
    result = classify_candidate_environment(50.0, 18.4, features)  # ~55 km pryč
    assert result.near_water is False
    assert result.nearest_water_km > NEAR_WATER_KM


def test_classify_no_features_in_region_gives_false_and_none_not_fake():
    features = _features()  # žádná voda v regionu vůbec nalezena
    result = classify_candidate_environment(49.5, 18.4, features)
    assert result.near_water is False
    assert result.nearest_water_km is None  # None, ne 0 ani nekonečno - poctivě "nevíme/nenalezeno"


def test_classify_forest_and_city_thresholds_independent():
    features = _features(forest=[(49.5, 18.4)], city=[(49.5, 18.4)])
    close = classify_candidate_environment(49.501, 18.4, features)
    assert close.near_forest is True
    assert close.near_city is True

    mid_distance = classify_candidate_environment(49.52, 18.4, features)  # ~2.2 km - mezi forest a city prahem
    assert mid_distance.near_forest is False  # nad NEAR_FOREST_KM (1.5)
    assert mid_distance.near_city is True  # pod NEAR_CITY_KM (6.0)
