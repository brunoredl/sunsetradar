"""Krok 2 (DEM kvalita a infrastruktura): abstrakce nad zdrojem výškových
dat. Terrain engine (`app/horizon.py`, `app/visibility.py`, `app/terrain_attributes.py`)
komunikuje jen s `DEMProvider` protokolem (`elevation(lat, lon)`,
`contains(lat, lon)`) - neví a nemusí vědět, jestli data pochází z
Copernicus GLO-30 (S3, GeoTIFF), ČÚZK DMR 5G (ArcGIS ImageServer,
reprojekce ze S-JTSK), nebo odjinud.

Nepřekomplikováno: `app.dem.DEMSource` (existující GeoTIFF wrapper) UŽ
implementuje přesně tenhle protokol, beze změny - obě konkrétní
implementace (`CopernicusDEMProvider`, `CuzkDMR5GProvider`) jsou jen
tenké "acquire the right file, pak ho otevři jako DEMSource" obálky nad
už existujícím `app/tile_manager.py` a `app/cuzk_dem.py`."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from app.dem import DEMSource


@runtime_checkable
class DEMProvider(Protocol):
    """Cokoliv, co umí odpovědět na 'jaká je nadmořská výška tady' a 'mám
    pro tenhle bod vůbec data' - žádný jiný požadavek na terrain engine."""

    def elevation(self, lat: float, lon: float) -> float: ...

    def contains(self, lat: float, lon: float) -> bool: ...


def open_copernicus_dem(lat: float, lon: float, radius_m: float, cache=None) -> DEMSource:
    """`DEMProvider` zdrojovaný z Copernicus DEM GLO-30 (viz app/tile_manager.py)."""
    from app.tile_manager import get_dem_path_for_area

    path = get_dem_path_for_area(lat, lon, radius_m, cache=cache)
    return DEMSource(path)


def open_cuzk_dmr5g_dem(lat: float, lon: float, radius_m: float, pixel_size_m: float = 10.0, cache=None) -> DEMSource:
    """`DEMProvider` zdrojovaný z ČÚZK DMR 5G (viz app/cuzk_dem.py). Funguje
    jen pro body v ČR - `app.cuzk_dem.OutsideCoverageError` mimo pokrytí."""
    from app.cuzk_dem import get_dem_path_for_area

    path = get_dem_path_for_area(lat, lon, radius_m, pixel_size_m=pixel_size_m, cache=cache)
    return DEMSource(path)
