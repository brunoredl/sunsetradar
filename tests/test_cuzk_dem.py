"""Testy pro app/cuzk_dem.py. Čisté funkce (bbox, pixel dims, efektivní
rozlišení) jsou rychlé jednotkové testy bez sítě. Skutečné stažení je
`@pytest.mark.integration` (viz Krok 11 - odděleno od unit testů)."""

from __future__ import annotations

import pytest

from app.cuzk_dem import (
    MAX_TOTAL_PIXELS,
    OutsideCoverageError,
    effective_pixel_size,
    bbox_for_area,
    pixel_dims_for_bbox,
)


def test_bbox_for_area_is_roughly_symmetric_around_point():
    west, south, east, north = bbox_for_area(50.5552, 13.9312, radius_m=10_000)
    assert west < 13.9312 < east
    assert south < 50.5552 < north
    # přibližná symetrie (na tyhle vzdálenosti zanedbáváme zakřivení)
    assert abs((13.9312 - west) - (east - 13.9312)) < 0.001


def test_pixel_dims_scale_with_area_and_resolution():
    bbox = bbox_for_area(50.5552, 13.9312, radius_m=10_000)
    width_10m, height_10m = pixel_dims_for_bbox(bbox, pixel_size_m=10.0)
    width_20m, height_20m = pixel_dims_for_bbox(bbox, pixel_size_m=20.0)
    # dvojnásobný pixel = přibližně poloviční rozměr v pixelech
    assert abs(width_10m / 2 - width_20m) < 5
    assert abs(height_10m / 2 - height_20m) < 5


def test_effective_pixel_size_unchanged_when_within_limits():
    bbox = bbox_for_area(50.5552, 13.9312, radius_m=3_000)  # malá oblast
    assert effective_pixel_size(bbox, requested_pixel_size_m=10.0) == 10.0


def test_effective_pixel_size_coarsened_when_exceeding_total_pixel_budget():
    bbox = bbox_for_area(50.5552, 13.9312, radius_m=30_000)  # velká oblast
    effective = effective_pixel_size(bbox, requested_pixel_size_m=10.0)
    assert effective > 10.0

    width_px, height_px = pixel_dims_for_bbox(bbox, effective)
    assert width_px * height_px <= MAX_TOTAL_PIXELS * 1.1  # malá rezerva na zaokrouhlení


def test_outside_czech_republic_rejected():
    from app.cuzk_dem import get_dem_path_for_area

    with pytest.raises(OutsideCoverageError):
        get_dem_path_for_area(48.8566, 2.3522, radius_m=5_000)  # Paříž


@pytest.mark.integration
def test_real_download_and_cache_hit(tmp_path):
    from app.cuzk_dem import CuzkDMR5GCache, get_dem_path_for_area
    from app.dem import DEMSource

    cache = CuzkDMR5GCache(cache_dir=tmp_path)
    path = get_dem_path_for_area(50.5552, 13.9312, radius_m=3_000, cache=cache)

    with DEMSource(path) as dem:
        elevation = dem.elevation(50.5552, 13.9312)
    assert 800 < elevation < 870  # Milešovka, DTM i DSM se shodují v tomhle rozsahu

    def _fail_if_called(*args, **kwargs):
        raise AssertionError("druhé volání nemělo stahovat znovu - mělo trefit cache")

    import app.cuzk_dem as cuzk_dem

    original_get = cuzk_dem.requests.get
    cuzk_dem.requests.get = _fail_if_called
    try:
        path2 = get_dem_path_for_area(50.5552, 13.9312, radius_m=3_000, cache=cache)
    finally:
        cuzk_dem.requests.get = original_get
    assert path2 == path
