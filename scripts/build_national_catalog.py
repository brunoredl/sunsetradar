"""Milestone "Data Foundation" - SKUTEČNÝ import celostátního candidate
katalogu (zadání bod 3/5/12 - "nechci jen mechanismus, co to umí,
skutečně to importuj"). Nahrazuje `scripts/build_candidate_catalog.py`
(3 ručně vybrané regiony -> `data/candidate_catalog.json`) jako zdroj
pravdy - tenhle skript pokrývá CELÉ ČR přes deterministickou mřížku
(`app/national_grid.py`) a zapisuje do PERZISTENTNÍ SQLite
(`app/catalog_db.py`), ne do JSON.

Spouští se RUČNĚ (`python scripts/build_national_catalog.py`).
Resumovatelné: každá dlaždice (candidates+places i environment) se
cachuje zvlášť na disk (`app/national_catalog.py`/`app/environment_context.py`)
- opakovaný běh přeskočí, co už je stažené, a jen doplní chybějící/
předtím selhané dlaždice (zadání bod 5 - "pokračuj po failed requestu").

ÚMYSLNĚ NESPOUŠTÍ terrain analysis (zadání bod 9) - jen rozšiřuje
katalog, existující terrain cache zůstává nedotčená; noví kandidáti
mohou být bez terrain cache, to řeší samostatný
`scripts/precompute_terrain.py` (běží nezávisle na pozadí)."""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import catalog_db
from app.candidate_discovery import CZECHIA_BBOX
from app.environment_context import discover_environment_features_cached
from app.national_catalog import NationalDiscoveryError, build_candidate_rows, discover_grid_cell_cached
from app.national_grid import generate_grid_cells
from app.slugs import assign_slugs

# Fair-use pauza mezi Overpass požadavky (zadání bod 5 - "retry při
# transient errors" implikuje i "nebuš do serveru bez pauzy") - jen
# před SKUTEČNÝMI síťovými požadavky, cache hit žádnou pauzu nepotřebuje.
POLITE_DELAY_S = 6.0

TYPE_LABELS_ORDER = ("peak", "viewpoint", "hill", "tower", "castle")


def main() -> None:
    t0 = time.time()
    cells = generate_grid_cells()
    print(f"Národní mřížka: {len(cells)} dlaždic nad {CZECHIA_BBOX}\n")

    all_candidates = []
    all_places = []
    cell_environment: dict[str, object] = {}
    failed_candidate_cells: list[str] = []
    failed_environment_cells: list[str] = []

    for i, cell in enumerate(cells):
        print(f"[{i + 1}/{len(cells)}] {cell.key} {cell.bbox}", flush=True)

        candidates_cache_hit = (
            Path("data/overpass_cache/national_grid") / f"{cell.key}.json"
        ).exists()
        try:
            result = discover_grid_cell_cached(cell.key, cell.bbox)
            all_candidates.extend(result.candidates)
            all_places.extend(result.places)
            print(f"    candidates+places: {len(result.candidates)} candidates, {len(result.places)} places"
                  f"{' (cache)' if candidates_cache_hit else ''}")
        except NationalDiscoveryError as exc:
            print(f"    FAILED (candidates+places): {exc}")
            failed_candidate_cells.append(cell.key)
        if not candidates_cache_hit:
            time.sleep(POLITE_DELAY_S)

        env_cache_dir = Path("data/overpass_cache")
        env_cache_hit = (env_cache_dir / f"{cell.key}_environment.json").exists()
        try:
            cell_environment[cell.key] = discover_environment_features_cached(cell.key, cell.bbox)
            print(f"    environment: ok{' (cache)' if env_cache_hit else ''}")
        except Exception as exc:  # noqa: BLE001 - stejná filozofie jako terrain_worker: nikdy nespadni na jedné dlaždici
            print(f"    FAILED (environment): {exc}")
            cell_environment[cell.key] = None
            failed_environment_cells.append(cell.key)
        if not env_cache_hit:
            time.sleep(POLITE_DELAY_S)

    print(f"\nRaw candidates (před filtrem/dedup): {len(all_candidates)}")
    print(f"Raw named places (municipality+locality): {len(all_places)}")

    # Čistá agregační logika (filtr/dedup/name-resolution/environment)
    # žije v app/national_catalog.py::build_candidate_rows() - testováno
    # bez sítě, viz tests/test_national_catalog.py.
    rows, duplicates_removed = build_candidate_rows(all_candidates, all_places, cell_environment)
    print(f"Po bbox filtru + globální deduplikaci: {len(rows)} (odstraněno {duplicates_removed} duplicit)")

    assign_slugs(rows)

    catalog_db.init_db()
    catalog_db.upsert_many(rows)

    dt = time.time() - t0
    by_type = catalog_db.count_by_type()
    by_source = catalog_db.count_by_name_source()
    total = catalog_db.count()
    with_municipality = sum(1 for r in rows if r["municipality"])
    with_cadastral = sum(1 for r in rows if r["cadastral_area"])

    print("\n" + "=" * 70)
    print("CELOSTÁTNÍ IMPORT - VÝSLEDEK")
    print("=" * 70)
    print(f"Total candidates: {total}")
    for t in TYPE_LABELS_ORDER:
        print(f"  {t.capitalize()}: {by_type.get(t, 0)}")
    other_types = set(by_type) - set(TYPE_LABELS_ORDER)
    for t in other_types:
        print(f"  {t.capitalize()}: {by_type[t]}")
    print(f"Named candidates (name_source=osm): {by_source.get('osm', 0)}")
    print(f"Generated-name candidates: {by_source.get('generated', 0)}")
    print(f"  (local: {by_source.get('local', 0)}, municipality: {by_source.get('municipality', 0)}, "
          f"cadastral: {by_source.get('cadastral', 0)})")
    print(f"Candidates with municipality: {with_municipality}")
    print(f"Candidates with cadastral area: {with_cadastral} (zdroj nezapojen tenhle milestone, viz PLAN)")
    print(f"Duplicates removed: {duplicates_removed}")
    print(f"Failed grid cells (candidates+places): {len(failed_candidate_cells)} {failed_candidate_cells}")
    print(f"Failed grid cells (environment): {len(failed_environment_cells)} {failed_environment_cells}")
    print(f"TOTAL COVERAGE: Czech Republic = {'YES' if not failed_candidate_cells else 'PARTIAL - viz failed cells výše'}")
    print(f"Build time: {dt:.1f}s")
    print(f"\nZapsáno do {catalog_db.DEFAULT_DB_PATH}")


if __name__ == "__main__":
    main()
