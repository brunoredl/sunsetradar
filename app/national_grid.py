"""Milestone "Data Foundation" - deterministická mřížka dlaždic
pokrývající celé ČR (zadání bod 5 - "Nespoléhej na jeden obrovský
Overpass request... použij deterministic grid"). Nahrazuje ruční výběr
tří regionů (`app/regions.py`) jako zdroj kandidátů - katalog teď
pokrývá celý stát, ne jen Beskydy/Krkonoše/Vsetínsko.

Velikost dlaždice (1.0° x 1.0°, cca 70-110 km hrana v ČR) zvolená jako
kompromis: dost malá, aby jeden Overpass dotaz zůstal rychlý a
spolehlivý (existující regiony už dřív úspěšně zvládaly srovnatelné
nebo větší rozpětí, viz app/regions.py), dost velká, aby celé ČR
pokrylo rozumným počtem requestů (21 pro CZECHIA_BBOX), ne stovkami -
zadání bod 5 varuje před příliš mnoha drobnými requesty stejně jako
před jedním obřím."""

from __future__ import annotations

from dataclasses import dataclass

from app.candidate_discovery import CZECHIA_BBOX

DEFAULT_CELL_SIZE_DEG = 1.0


@dataclass(frozen=True)
class GridCell:
    key: str
    bbox: tuple[float, float, float, float]  # south, west, north, east


def generate_grid_cells(
    bbox: tuple[float, float, float, float] = CZECHIA_BBOX,
    cell_size_deg: float = DEFAULT_CELL_SIZE_DEG,
) -> list[GridCell]:
    """Rozdělí `bbox` na dlaždice o hraně `cell_size_deg` (poslední
    řádek/sloupec může být menší, pokud rozpětí není celočíselný
    násobek - žádná dlaždice nepřesahuje `bbox`). Deterministické -
    stejný `bbox`/`cell_size_deg` vždy vygeneruje STEJNÉ dlaždice ve
    STEJNÉM pořadí (zadání bod 6/11 - "deduplikace/import musí být
    deterministický")."""
    south, west, north, east = bbox
    cells: list[GridCell] = []
    lat = south
    row = 0
    while lat < north:
        cell_north = min(lat + cell_size_deg, north)
        lon = west
        col = 0
        while lon < east:
            cell_east = min(lon + cell_size_deg, east)
            key = f"grid_{row:02d}_{col:02d}"
            cells.append(GridCell(key=key, bbox=(lat, lon, cell_north, cell_east)))
            lon = cell_east
            col += 1
        lat = cell_north
        row += 1
    return cells
