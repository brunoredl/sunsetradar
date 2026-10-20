"""Sdílené fixtures pro testy - syntetický (ne stažený) DEM v dočasném
souboru, aby testy nezávisely na `data/milesovka_dem.tif` (ten je
gitignored a nemusí být přítomný v CI)."""

from __future__ import annotations

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin

GRID_SIZE = 200
PIXEL_DEG = 0.0002
LAT0 = 50.0
LON0 = 14.0
BASE_ELEVATION = 500.0


@pytest.fixture
def flat_dem_path(tmp_path):
    """Úplně plochý terén (500 m všude) - pro test, že bez skutečné
    překážky vrátí terrain_angle rozumnou malou hodnotu (ne spurious
    velké číslo)."""
    path = tmp_path / "flat.tif"
    data = np.full((GRID_SIZE, GRID_SIZE), BASE_ELEVATION, dtype=np.float32)
    _write_dem(path, data)
    return str(path)


@pytest.fixture
def wall_dem_path(tmp_path):
    """Plochý terén s jednou vyvýšenou 'zdí' (800 m) o pár sloupců na
    západ od středu - pro test, že terrain_angle změří odpovídající úhel."""
    path = tmp_path / "wall.tif"
    data = np.full((GRID_SIZE, GRID_SIZE), BASE_ELEVATION, dtype=np.float32)
    center_col = GRID_SIZE // 2
    wall_col = center_col - 30  # 30 pixelů na západ od středu (~ desítky-stovky m, viz test)
    data[:, max(wall_col - 1, 0) : wall_col + 1] = BASE_ELEVATION + 300.0
    _write_dem(path, data)
    return str(path)


@pytest.fixture
def nodata_dem_path(tmp_path):
    """Plochý terén s pásem NODATA (-9999) pár desítek pixelů na západ -
    simuluje mezeru v datech (okraj dlaždice, vodní plocha apod.)."""
    path = tmp_path / "nodata.tif"
    data = np.full((GRID_SIZE, GRID_SIZE), BASE_ELEVATION, dtype=np.float32)
    center_col = GRID_SIZE // 2
    void_col = center_col - 30
    data[:, max(void_col - 1, 0) : void_col + 1] = -9999.0
    _write_dem(path, data, nodata=-9999.0)
    return str(path)


@pytest.fixture(autouse=True)
def _reset_rate_limiters():
    """Příprava bezplatného testovacího nasazení - `/api/analyze`,
    `/api/find_sunset` a `/api/geocode` teď mají per-IP rate limiter
    (`app/main.py`, zadání bod 5 - "veřejný provoz nemůže spouštět
    neomezeně drahé výpočty"). Testy sdílejí JEDEN `TestClient` napříč
    celým modulem (viz tests/test_web.py `client = TestClient(...)`),
    takže by bez resetu brzy narazily na stejný limit jako skutečný
    útočník - limiter funguje správně, jen testy musí mezi sebou
    dostat čistý stav (produkční chování se nemění, limit pořád
    skutečně platí v rámci jednoho běžícího procesu)."""
    import app.main as main_module

    for limiter in (main_module._analyze_limiter, main_module._find_sunset_limiter, main_module._geocode_limiter):
        limiter._hits.clear()
    yield


def _write_dem(path, data: np.ndarray, nodata: float | None = None) -> None:
    half = GRID_SIZE * PIXEL_DEG / 2
    transform = from_origin(LON0 - half, LAT0 + half, PIXEL_DEG, PIXEL_DEG)
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        height=data.shape[0],
        width=data.shape[1],
        count=1,
        dtype=data.dtype,
        crs="EPSG:4326",
        transform=transform,
        nodata=nodata,
    ) as dst:
        dst.write(data, 1)
