"""Testy pro app/terrain_attributes.py - syntetické nakloněné roviny se
ZNÁMÝM sklonem/expozicí (lze spočítat analyticky), aby šlo ověřit, že
`compute_slope_aspect` vrací fyzikálně správný výsledek, ne jen "něco
rozumně vypadajícího"."""

from __future__ import annotations

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin

from app.dem import DEMSource
from app.geo import geodesic_distance_m
from app.terrain_attributes import compute_slope_aspect

GRID_SIZE = 60
PIXEL_DEG = 0.0005
LAT0, LON0 = 50.0, 14.0
BASE_ELEVATION = 500.0


def _make_tilted_plane(tmp_path, gradient_per_degree_lat: float, gradient_per_degree_lon: float) -> str:
    """Rovina: elevation = BASE + gradient_lat*(lat-LAT0) + gradient_lon*(lon-LON0)."""
    half = GRID_SIZE * PIXEL_DEG / 2
    transform = from_origin(LON0 - half, LAT0 + half, PIXEL_DEG, PIXEL_DEG)

    data = np.zeros((GRID_SIZE, GRID_SIZE), dtype=np.float32)
    for row in range(GRID_SIZE):
        for col in range(GRID_SIZE):
            lon, lat = transform * (col + 0.5, row + 0.5)
            data[row, col] = BASE_ELEVATION + gradient_per_degree_lat * (lat - LAT0) + gradient_per_degree_lon * (lon - LON0)

    path = tmp_path / "tilted.tif"
    with rasterio.open(
        path, "w", driver="GTiff", height=GRID_SIZE, width=GRID_SIZE, count=1,
        dtype=data.dtype, crs="EPSG:4326", transform=transform,
    ) as dst:
        dst.write(data, 1)
    return str(path)


def test_north_facing_slope(tmp_path):
    # elevace ROSTE se zeměpisnou šířkou -> na sever je "do kopce" ->
    # spádnice (dolů) míří na JIH -> aspekt by měl být ~180°.
    path = _make_tilted_plane(tmp_path, gradient_per_degree_lat=50_000.0, gradient_per_degree_lon=0.0)
    with DEMSource(path) as dem:
        result = compute_slope_aspect(dem, LAT0, LON0, sample_distance_m=50.0)
    assert 170 < result.aspect_deg < 190
    assert result.slope_deg > 0


def test_south_facing_slope(tmp_path):
    # elevace KLESÁ se zeměpisnou šířkou -> na sever je "z kopce" ->
    # spádnice míří na SEVER -> aspekt ~0°/360°.
    path = _make_tilted_plane(tmp_path, gradient_per_degree_lat=-50_000.0, gradient_per_degree_lon=0.0)
    with DEMSource(path) as dem:
        result = compute_slope_aspect(dem, LAT0, LON0, sample_distance_m=50.0)
    assert result.aspect_deg < 10 or result.aspect_deg > 350


def test_east_facing_slope(tmp_path):
    # elevace ROSTE s délkou -> na východ je do kopce -> spádnice na ZÁPAD -> aspekt ~270°.
    path = _make_tilted_plane(tmp_path, gradient_per_degree_lat=0.0, gradient_per_degree_lon=50_000.0)
    with DEMSource(path) as dem:
        result = compute_slope_aspect(dem, LAT0, LON0, sample_distance_m=50.0)
    assert 260 < result.aspect_deg < 280


def test_flat_terrain_has_near_zero_slope(tmp_path):
    path = _make_tilted_plane(tmp_path, gradient_per_degree_lat=0.0, gradient_per_degree_lon=0.0)
    with DEMSource(path) as dem:
        result = compute_slope_aspect(dem, LAT0, LON0, sample_distance_m=50.0)
    assert result.slope_deg < 0.5


def test_slope_magnitude_matches_known_gradient(tmp_path):
    # gradient 1000 m nadmořské výšky na stupeň šířky (~111 km) => sklon
    # atan(1000/111000) ~ 0.516° - ověříme řádovou shodu (tolerance kvůli
    # diskretizaci/sample_distance).
    path = _make_tilted_plane(tmp_path, gradient_per_degree_lat=1000.0, gradient_per_degree_lon=0.0)
    with DEMSource(path) as dem:
        result = compute_slope_aspect(dem, LAT0, LON0, sample_distance_m=100.0)
    import math

    expected_slope = math.degrees(math.atan(1000.0 / 111_320.0))
    assert abs(result.slope_deg - expected_slope) < 0.1
