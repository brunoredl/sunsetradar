"""Krok 7: DEM tile manager - pro GPS bod + poloměr analýzy zjistí, které
1°×1° Copernicus DEM dlaždice jsou potřeba, stáhne jen ty chybějící do
lokální cache, a pokud je potřeba víc dlaždic najednou (bod blízko švu
mezi dlaždicemi + širší poloměr), sloučí je do jedné dočasné GeoTIFF
mozaiky (`rasterio.merge`), kterou pak `app/dem.py::DEMSource` otevře
úplně stejně jako jednu staženou dlaždici - žádná změna v DEMSource nebyla
potřeba.

Cíleně JEDNODUCHÉ (viz zadání "nepřeháněj architekturu"): žádná databáze,
žádné TTL/expirace cache (Copernicus DEM se nemění), jen "je soubor na
disku? Ne -> stáhni. Ano -> použij."
"""

from __future__ import annotations

import logging
import math
from pathlib import Path

import requests

logger = logging.getLogger(__name__)

S3_BASE_URL = "https://copernicus-dem-30m.s3.amazonaws.com"
DEFAULT_CACHE_DIR = Path(__file__).resolve().parent.parent / "data" / "dem_cache"

# Poloměr Země pro hrubý převod metrů na stupně při odhadu potřebných
# dlaždic - stačí konzervativní odhad (mírně nadhodnocený obalový
# obdélník), přesnost tady neřeší nic kritického.
_METERS_PER_DEGREE_LAT = 111_320.0


def tile_id_for(lat: float, lon: float) -> str:
    """Copernicus DEM GLO-30 pojmenování dlaždic - 1°×1° grid, název podle
    podlahové (floor) celočíselné souřadnice jihozápadního rohu. Ověřeno
    empiricky přes tileList.txt v S3 bucketu (viz PLAN.md) - skutečný
    prefix je "COG_10", ne "COG_30", jak by název bucketu naznačoval."""
    lat_floor = math.floor(lat)
    lon_floor = math.floor(lon)
    ns = "N" if lat_floor >= 0 else "S"
    ew = "E" if lon_floor >= 0 else "W"
    return f"Copernicus_DSM_COG_10_{ns}{abs(lat_floor):02d}_00_{ew}{abs(lon_floor):03d}_00_DEM"


def tiles_needed_for_area(lat: float, lon: float, radius_m: float) -> list[str]:
    """Vrátí seznam ID všech 1°×1° dlaždic, které se (aspoň částečně)
    překrývají s kruhem o daném poloměru kolem (lat, lon)."""
    lat_margin = radius_m / _METERS_PER_DEGREE_LAT
    lon_margin = radius_m / (_METERS_PER_DEGREE_LAT * max(math.cos(math.radians(lat)), 0.01))

    lat_min, lat_max = lat - lat_margin, lat + lat_margin
    lon_min, lon_max = lon - lon_margin, lon + lon_margin

    tiles = set()
    for lat_floor in range(math.floor(lat_min), math.floor(lat_max) + 1):
        for lon_floor in range(math.floor(lon_min), math.floor(lon_max) + 1):
            tiles.add(tile_id_for(lat_floor + 0.5, lon_floor + 0.5))
    return sorted(tiles)


class DEMTileCache:
    """Stará se o stažení a lokální cache Copernicus DEM dlaždic."""

    def __init__(self, cache_dir: str | Path = DEFAULT_CACHE_DIR):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def tile_path(self, tile_id: str) -> Path:
        return self.cache_dir / f"{tile_id}.tif"

    def ensure_local(self, tile_id: str, timeout_s: float = 120.0) -> Path:
        """Vrátí lokální cestu k dlaždici - stáhne ji, jen pokud tam ještě
        není (žádné opakované stahování už jednou stažených dat)."""
        path = self.tile_path(tile_id)
        if path.exists():
            logger.info("Dlaždice %s už je v cache (%s), stahování přeskočeno.", tile_id, path)
            return path

        url = f"{S3_BASE_URL}/{tile_id}/{tile_id}.tif"
        logger.info("Stahuji chybějící dlaždici %s z %s ...", tile_id, url)
        response = requests.get(url, timeout=timeout_s, stream=True)
        response.raise_for_status()

        tmp_path = path.with_suffix(".tif.part")
        with open(tmp_path, "wb") as f:
            for chunk in response.iter_content(chunk_size=1 << 20):
                f.write(chunk)
        tmp_path.rename(path)  # atomický přesun - nikdy nenecháme napůl stažený soubor pod finálním jménem
        return path

    def ensure_local_many(self, tile_ids: list[str]) -> list[Path]:
        return [self.ensure_local(tile_id) for tile_id in tile_ids]


def get_dem_path_for_area(
    lat: float,
    lon: float,
    radius_m: float,
    cache: DEMTileCache | None = None,
) -> str:
    """Hlavní vstupní bod (Krok 7): zjistí potřebné dlaždice, stáhne
    chybějící, a vrátí CESTU K SOUBORU, který lze rovnou otevřít
    `app.dem.DEMSource` - buď přímo jedna stažená dlaždice (běžný případ),
    nebo sloučená mozaika (bod blízko švu + širší poloměr)."""
    cache = cache or DEMTileCache()
    tile_ids = tiles_needed_for_area(lat, lon, radius_m)
    paths = cache.ensure_local_many(tile_ids)

    if len(paths) == 1:
        return str(paths[0])

    return _merge_tiles(paths, cache.cache_dir, tile_ids)


def _merge_tiles(paths: list[Path], cache_dir: Path, tile_ids: list[str]) -> str:
    from app.dem_merge import merge_geotiffs

    merged_name = "merged_" + "_".join(sorted(t.replace("Copernicus_DSM_COG_10_", "") for t in tile_ids)) + ".tif"
    return merge_geotiffs(paths, cache_dir / merged_name)
