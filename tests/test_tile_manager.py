"""Testy pro app/tile_manager.py (Krok 7). Čisté funkce (`tile_id_for`,
`tiles_needed_for_area`) jsou rychlé jednotkové testy bez sítě. Skutečné
stahování/cache/merge je označené `@pytest.mark.integration` (potřebuje
síť nebo existující lokální DEM) - spustit zvlášť: `pytest -m integration`
(viz Krok 11 zadání: "pokud test závisí na internetu, odděl ho")."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.tile_manager import DEMTileCache, tile_id_for, tiles_needed_for_area

DATA_DIR = Path(__file__).resolve().parent.parent / "data"


def test_tile_id_matches_known_milesovka_tile():
    assert tile_id_for(50.5552, 13.9312) == "Copernicus_DSM_COG_10_N50_00_E013_00_DEM"


def test_tile_id_matches_known_krusne_hory_tile():
    assert tile_id_for(50.3972, 12.9479) == "Copernicus_DSM_COG_10_N50_00_E012_00_DEM"


def test_tile_id_handles_southern_and_western_hemisphere():
    assert tile_id_for(-33.5, -70.5) == "Copernicus_DSM_COG_10_S34_00_W071_00_DEM"


def test_single_tile_sufficient_for_small_radius():
    # Milešovka (13.93°E) je ~4.8 km od hranice dlaždice na 14.0°E - na
    # krátký dosah (3 km) je jedna dlaždice dost, na 30 km už ne (viz níže).
    tiles = tiles_needed_for_area(50.5552, 13.9312, radius_m=3_000)
    assert tiles == ["Copernicus_DSM_COG_10_N50_00_E013_00_DEM"]


def test_wide_radius_near_tile_edge_pulls_in_neighbour():
    # stejný bod, ale s dosahem 30 km už zabere i sousední dlaždici na východě
    tiles = tiles_needed_for_area(50.5552, 13.9312, radius_m=30_000)
    assert "Copernicus_DSM_COG_10_N50_00_E013_00_DEM" in tiles
    assert "Copernicus_DSM_COG_10_N50_00_E014_00_DEM" in tiles


def test_multiple_tiles_needed_near_boundary():
    # bod těsně u hranice 13.0°E se širším poloměrem musí zabrat i sousední dlaždici
    tiles = tiles_needed_for_area(50.5, 13.02, radius_m=30_000)
    assert "Copernicus_DSM_COG_10_N50_00_E012_00_DEM" in tiles
    assert "Copernicus_DSM_COG_10_N50_00_E013_00_DEM" in tiles


def test_tiles_needed_near_integer_latitude_boundary():
    tiles = tiles_needed_for_area(51.02, 13.5, radius_m=30_000)
    assert "Copernicus_DSM_COG_10_N50_00_E013_00_DEM" in tiles
    assert "Copernicus_DSM_COG_10_N51_00_E013_00_DEM" in tiles


@pytest.mark.integration
def test_ensure_local_skips_redownload_when_already_cached(tmp_path):
    """Nakopíruje už staženou reálnou dlaždici (data/milesovka_dem.tif) do
    čerstvé cache pod očekávaným jménem a ověří, že `ensure_local` NEvolá
    žádný network request (protože soubor už tam je)."""
    import shutil

    tile_id = "Copernicus_DSM_COG_10_N50_00_E013_00_DEM"
    cache = DEMTileCache(cache_dir=tmp_path)
    shutil.copy(DATA_DIR / "milesovka_dem.tif", cache.tile_path(tile_id))

    def _fail_if_called(*args, **kwargs):
        raise AssertionError("ensure_local neměl stahovat - soubor už byl v cache")

    import app.tile_manager as tm

    original_get = tm.requests.get
    tm.requests.get = _fail_if_called
    try:
        path = cache.ensure_local(tile_id)
    finally:
        tm.requests.get = original_get

    assert path == cache.tile_path(tile_id)
    assert path.exists()


@pytest.mark.integration
def test_get_dem_path_merges_two_real_tiles(tmp_path):
    """Sloučí dvě už stažené reálné dlaždice (Milešovka + Krušné hory) a
    ověří, že výsledná mozaika jde otevřít a dá smysluplnou výšku na obou
    stranách švu."""
    import shutil

    from app.dem import DEMSource
    from app.tile_manager import get_dem_path_for_area

    cache = DEMTileCache(cache_dir=tmp_path)
    shutil.copy(DATA_DIR / "milesovka_dem.tif", cache.tile_path("Copernicus_DSM_COG_10_N50_00_E013_00_DEM"))
    shutil.copy(DATA_DIR / "krusne_hory_dem.tif", cache.tile_path("Copernicus_DSM_COG_10_N50_00_E012_00_DEM"))

    merged_path = get_dem_path_for_area(50.5, 13.02, radius_m=40_000, cache=cache)

    with DEMSource(merged_path) as dem:
        elev_east_tile = dem.elevation(50.5552, 13.9312)  # Milešovka, v E013
        elev_west_tile = dem.elevation(50.3972, 12.9479)  # Krušné hory, v E012

    assert 800 < elev_east_tile < 870
    assert 1000 < elev_west_tile < 1150
