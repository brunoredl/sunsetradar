"""Regresní testy pro app/dem.py - Krok 1 auditu odhalil, že `elevation()`
tiše "clampovala" body mimo hranice DEM na okrajový pixel místo chyby, a
NODATA pixely se vůbec nekontrolovaly (bilineárně by se zprůměrovaly do
"věrohodně" vypadající, ale smyšlené výšky)."""

from __future__ import annotations

import math

import pytest

from app.dem import DEMSource, OutOfBoundsError


def test_elevation_inside_bounds_works(flat_dem_path):
    with DEMSource(flat_dem_path) as dem:
        assert dem.elevation(50.0, 14.0) == pytest.approx(500.0)


def test_elevation_outside_bounds_raises_not_silently_clamps(flat_dem_path):
    with DEMSource(flat_dem_path) as dem:
        b = dem.bounds
        with pytest.raises(OutOfBoundsError):
            dem.elevation(b.top + 1.0, 14.0)  # daleko za severní hranicí
        with pytest.raises(OutOfBoundsError):
            dem.elevation(50.0, b.right + 1.0)  # daleko za východní hranicí


def test_bounds_is_cached_not_recomputed_every_call(flat_dem_path):
    """Krok 8 (profiling) odhalil, že `rasterio` dataset `.bounds` je
    drahé přepočítávat při každém volání `contains()` (~70 % celého
    výpočtu 360° horizontu). Regresní test: opakované přístupy k
    `dem.bounds` musí vracet STEJNOU (cachovanou) instanci, ne pokaždé
    nový objekt počítaný z GDAL transformace."""
    with DEMSource(flat_dem_path) as dem:
        assert dem.bounds is dem.bounds


def test_contains_matches_elevation_behaviour(flat_dem_path):
    with DEMSource(flat_dem_path) as dem:
        b = dem.bounds
        assert dem.contains(50.0, 14.0) is True
        assert dem.contains(b.top + 1.0, 14.0) is False


def test_nodata_pixel_returns_nan_not_fabricated_value(nodata_dem_path):
    with DEMSource(nodata_dem_path) as dem:
        # bod přesně v NODATA pásu (30 pixelů na západ od středu, viz fixture)
        value = dem.elevation(50.0, 14.0 - 30 * 0.0002)
        assert math.isnan(value)


def test_nodata_does_not_leak_into_neighbouring_valid_pixels(nodata_dem_path):
    with DEMSource(nodata_dem_path) as dem:
        # bod daleko od NODATA pásu musí být stále čisté číslo
        value = dem.elevation(50.0, 14.0 + 0.01)
        assert not math.isnan(value)
        assert value == pytest.approx(500.0)
