"""Testy pro app/place_names.py (milestone "Data Foundation" - name
resolution pipeline, zadání bod 1/11)."""

from __future__ import annotations

from app.national_catalog import PlaceRecord
from app.place_names import (
    MUNICIPALITY_SEARCH_KM,
    PLACE_BUCKET_SIZE_DEG,
    PlaceIndex,
    resolve_cadastral_area,
    resolve_display_name,
    resolve_nearby_places,
    resolve_osm_name,
)


def test_resolve_osm_name_prefers_plain_name_tag():
    assert resolve_osm_name({"name": "Radhošť", "alt_name": "Something else"}) == "Radhošť"


def test_resolve_osm_name_falls_back_to_alternate_tags():
    assert resolve_osm_name({"official_name": "Rozhledna Bílý kříž"}) == "Rozhledna Bílý kříž"


def test_resolve_osm_name_returns_none_when_no_tag_present():
    assert resolve_osm_name({"natural": "peak"}) is None


def test_resolve_osm_name_rejects_coordinate_looking_value():
    # obranná kontrola (zadání bod 11 - "coordinate-only name rejection")
    assert resolve_osm_name({"name": "49.5459, 18.4472"}) is None


def test_resolve_nearby_places_splits_by_tier():
    places = [
        PlaceRecord(name="Frenštát pod Radhoštěm", lat=49.55, lon=18.21, tier="municipality"),
        PlaceRecord(name="Bystré", lat=49.50, lon=18.23, tier="locality"),
    ]
    resolved = resolve_nearby_places(49.5459784, 18.4472696, places)
    assert resolved.municipality.name == "Frenštát pod Radhoštěm"
    assert resolved.locality.name == "Bystré"


def test_resolve_nearby_places_returns_none_when_no_places_in_tier():
    resolved = resolve_nearby_places(49.5, 18.4, [])
    assert resolved.municipality is None
    assert resolved.locality is None


def test_resolve_display_name_uses_osm_name_when_present():
    name, source = resolve_display_name({"name": "Radhošť"}, "peak", 49.5, 18.4, [])
    assert name == "Radhošť"
    assert source == "osm"


def test_resolve_display_name_uses_local_tier_within_threshold():
    places = [PlaceRecord(name="Bystré", lat=49.501, lon=18.401, tier="locality")]
    name, source = resolve_display_name({}, "viewpoint", 49.5, 18.4, places)
    assert name == "Vyhlídka – Bystré"
    assert source == "local"


def test_resolve_display_name_falls_back_to_municipality_when_no_close_locality():
    # Zadání dává příklad "Vyhlídka nad Valašským Meziříčím" - skutečný
    # kód drží jméno sídla v 1. pádě (spojovník místo předložky), protože
    # obecné skloňování libovolného českého vlastního jména bez AI/nové
    # gramatické závislosti nejde spolehlivě zaručit (viz komentář u
    # resolve_display_name) - pořád stejný koncept/kvalita, jen
    # gramaticky robustnější tvar.
    places = [PlaceRecord(name="Valašské Meziříčí", lat=49.47, lon=17.97, tier="municipality")]
    name, source = resolve_display_name({}, "viewpoint", 49.5, 18.0, places)
    assert name == "Vyhlídka – Valašské Meziříčí"
    assert source == "municipality"


def test_resolve_display_name_prefers_local_over_municipality_when_both_available():
    places = [
        PlaceRecord(name="Bystré", lat=49.501, lon=18.401, tier="locality"),
        PlaceRecord(name="Frenštát pod Radhoštěm", lat=49.55, lon=18.21, tier="municipality"),
    ]
    name, source = resolve_display_name({}, "peak", 49.5, 18.4, places)
    assert source == "local"
    assert "Bystré" in name


def test_resolve_display_name_never_returns_raw_coordinates():
    name, source = resolve_display_name({}, "peak", 12.3456, 98.7654, [])  # daleko od jakéhokoliv sídla v testu
    assert "12.3456" not in name
    assert "98.7654" not in name
    assert source == "generated"


def test_resolve_display_name_generated_is_never_labeled_as_official_source():
    name, source = resolve_display_name({}, "hill", 12.3456, 98.7654, [])
    assert source != "osm"
    assert source != "municipality"
    assert source != "local"


def test_resolve_cadastral_area_always_none_this_milestone():
    # zdokumentované omezení tohohle kroku (viz modul docstring) - ne bug
    assert resolve_cadastral_area(49.5, 18.4) is None


# --- PlaceIndex (výkonnostní oprava - živě naměřeno ~7,9h na naivním
# O(kandidáti × místa) skenu nad celostátním katalogem, viz modul
# docstring u PLACE_BUCKET_SIZE_DEG) ---


def test_place_index_finds_same_nearest_as_linear_scan():
    places = [
        PlaceRecord(name="Blízké", lat=49.501, lon=18.401, tier="municipality"),
        PlaceRecord(name="Daleké", lat=50.5, lon=14.0, tier="municipality"),
        PlaceRecord(name="Lokalita", lat=49.502, lon=18.402, tier="locality"),
    ]
    index = PlaceIndex(places)
    from_index = resolve_nearby_places(49.5, 18.4, index)
    from_list = resolve_nearby_places(49.5, 18.4, places)
    assert from_index.municipality.name == from_list.municipality.name == "Blízké"
    assert from_index.locality.name == from_list.locality.name == "Lokalita"


def test_place_index_covers_full_municipality_search_radius_at_bucket_edge():
    # Nejhorší případ - kandidát na kraji vlastního kbelíku, cílové místo
    # skoro přesně MUNICIPALITY_SEARCH_KM daleko - musí ho najít stejně
    # jako lineární sken (zadání bod 11 - "deterministic naming").
    bucket_edge_lat = PLACE_BUCKET_SIZE_DEG * 10 - 0.001  # těsně pod hranou kbelíku
    far_place = PlaceRecord(name="Na hraně dosahu", lat=bucket_edge_lat + (MUNICIPALITY_SEARCH_KM / 111.0) * 0.95, lon=18.0, tier="municipality")
    places = [far_place]
    index = PlaceIndex(places)
    from_index = resolve_nearby_places(bucket_edge_lat, 18.0, index)
    from_list = resolve_nearby_places(bucket_edge_lat, 18.0, places)
    assert from_index.municipality is not None
    assert from_index.municipality.name == from_list.municipality.name


def test_place_index_empty_places_returns_none():
    index = PlaceIndex([])
    result = resolve_nearby_places(49.5, 18.4, index)
    assert result.locality is None
    assert result.municipality is None


def test_resolve_display_name_accepts_precomputed_resolved_places_without_recomputing():
    # app.national_catalog.build_candidate_rows() počítá resolved_places
    # JEDNOU a předá ho dál - musí dát stejný výsledek jako bez něj.
    places = [PlaceRecord(name="Bystré", lat=49.501, lon=18.401, tier="locality")]
    resolved = resolve_nearby_places(49.5, 18.4, places)
    name, source = resolve_display_name({}, "peak", 49.5, 18.4, places, resolved_places=resolved)
    assert name == "Vrchol – Bystré"
    assert source == "local"
