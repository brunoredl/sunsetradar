"""Milestone "Persistent Terrain Cache + Background Precomputation",
rozšířeno Milestonem "Smart National Terrain Precompute" - offline CLI:
vybere kandidáty z centrálního katalogu (app/candidate_catalog.py, teď
přednostně celostátní SQLite, app/catalog_db.py) a naplní perzistentní
terrain cache (app/terrain_cache.py) VOLÁNÍM STEJNÉ `compute_static_terrain()`,
jakou používá i live search (`/api/search`, `/api/find_sunset`) a
background worker (`app/terrain_worker.py`) - žádný nový/duplicitní
terrain engine, cache ani systém identifikátorů, jen dávkové spuštění
nad stejnými daty.

Příklady:
    # Dřívější regionální režim (BEZE ZMĚNY v chování pro volajícího,
    # jen interně teď filtruje podle bbox regionu - viz "Regionální
    # kompatibilita" níže):
    python scripts/precompute_terrain.py --region beskydy
    python scripts/precompute_terrain.py --region krkonose --candidate-type peak --limit 50

    # Nové - celostátní katalog (žádný --region = celá ČR, 37k+ kandidátů):
    python scripts/precompute_terrain.py --limit 20 --dry-run
    python scripts/precompute_terrain.py --limit 20
    python scripts/precompute_terrain.py --candidate-type viewpoint --limit 30

    # Nové - geografická oblast zájmu (radius od souřadnic, využívá
    # STEJNOU `catalog_db.radius_search()` jako /api/search):
    python scripts/precompute_terrain.py --near-lat 50.0755 --near-lon 14.4378 --near-radius-km 25 --limit 20 --dry-run

    python scripts/precompute_terrain.py --region vsetinsko --force

Regionální kompatibilita: `--region` zůstává STEJNÝ argument/stejná 3
jména (beskydy/krkonose/vsetinsko) jako dřív. Dřív filtroval přesnou
shodu stringu v poli `region` kandidáta - to platilo, dokud `region`
znamenalo "jedno z těch 3 ručně vybraných jmen". Od celostátního
importu (`app/national_grid.py`) ale `region` u kandidátů z SQLite
katalogu nese jméno mřížkové dlaždice (`grid_01_02`...), ne starý
název - přesná shoda stringu by teď tiše vracela PRÁZDNÝ výsledek pro
`--region beskydy` (reálně ověřeno před touhle opravou). Filtr je proto
teď podle GEOGRAFICKÉHO bbox toho regionu (`app.regions.REGIONS`), ne
podle stringu v poli `region` - funguje správně bez ohledu na to, jak
je kandidát interně otagovaný.

"Smart" prioritizace (zadání bod 3, celé zdůvodnění v `prioritize_candidates()`
níže) - jednoduchá, deterministická, založená JEN na reálných datech
z katalogu (typ, mřížková dlaždice = geografická poloha, `near_city`),
NIKDY nová/odhadnutá veličina a NIKDY nemění `compute_sunset_rating()`/
`sunset_score` - ovlivňuje jen POŘADÍ, ve kterém TENHLE skript
kandidáty zpracovává."""

from __future__ import annotations

import argparse
import sys
import time
from itertools import zip_longest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import catalog_db, terrain_cache
from app.analyze_location import DEFAULT_PIXEL_SIZE_M, compute_static_terrain
from app.candidate_catalog import load_candidate_catalog
from app.regions import REGIONS

# Zadání bod 3 - "přednostně relevantní vrcholy, vyhlídky a rozhledny".
# Binární úroveň (ne jemnější žebříček uvnitř trojice - zadání sám
# nejmenuje pořadí mezi peak/viewpoint/tower, jen že mají jít PŘED
# hill/castle), beze změny `candidate_type` samotného.
_PRIORITY_TYPES = frozenset({"peak", "viewpoint", "tower"})


def _in_bbox(c: dict, bbox: tuple[float, float, float, float]) -> bool:
    south, west, north, east = bbox
    return south <= c["lat"] <= north and west <= c["lon"] <= east


def select_candidates(
    catalog: list[dict],
    cache_key: str,
    candidate_type: str | None = None,
    force: bool = False,
) -> tuple[list[dict], int, int]:
    """Filtr podle `candidate_type` (pokud zadán) + podle stavu terrain
    cache. Vrací (kandidáti_k_zpracování, přeskočeno_ready, přeskočeno_running).

    Přeskočuje VŽDY `running` (zadání bod 2/4 - "nesmí způsobit kolize
    s běžícím workerem"): `running` znamená, že na tenhle konkrétní
    (candidate_id, cache_key) pár PRÁVĚ NĚKDO jiný počítá - živý
    background worker (`app/terrain_worker.py`) při zpracování requestu
    z `/api/search`/`/api/find_sunset`, nebo jiný souběžný běh tohoto
    skriptu. Přepočítat ho znovu by bylo zbytečné zdvojení práce
    (nikdy ne poškození dat - `save_ready`/`save_failed` jsou idempotentní
    upserty), ale zadání bod 4 žádá se tomu vyhnout.

    `ready` se přeskakuje JEN bez `--force` (zadání bod 2 - "nepřepisuj
    platné záznamy bez explicitního --force"), `failed` se VŽDY zkouší
    znovu (zadání bod 2 - "nepředpokládej, že každá chyba je trvalá" -
    stejná filozofie jako živý worker, který failed taky nikdy
    trvale nezatracuje)."""
    if candidate_type:
        catalog = [c for c in catalog if c.get("candidate_type") == candidate_type]

    to_process: list[dict] = []
    skipped_ready = 0
    skipped_running = 0
    for c in catalog:
        status = terrain_cache.get_status(c["id"], cache_key)
        if status == terrain_cache.STATUS_RUNNING:
            skipped_running += 1
            continue
        if status == terrain_cache.STATUS_READY and not force:
            skipped_ready += 1
            continue
        to_process.append(c)
    return to_process, skipped_ready, skipped_running


def prioritize_candidates(candidates: list[dict]) -> list[dict]:
    """Deterministická, vysvětlitelná priorita BEZ nového skóre (zadání
    bod 3 - "nezaváděj umělou přesnost ani nový Sunset Score"):

    1. Skuteční kandidáti se GRUPUJÍ podle existujícího pole `region`
       (u celostátního katalogu = klíč mřížkové dlaždice z
       `app/national_grid.py`, např. "grid_01_02" - REÁLNÁ geografická
       poloha, ne vymyšlená). Chybějící/`None` region jde do vlastní
       skupiny `"_unknown"`.
    2. UVNITŘ každé skupiny se řadí podle (v tomhle pořadí, všechno
       existující atributy): typ je peak/viewpoint/tower? (zadání bod 3
       - "přednostně relevantní") -> `near_city` je `True`? (existující
       OSM-odvozený signál z `app/environment_context.py` - "praktická
       dostupnost", zadání bod 3 - jen pokud je `near_city` `None`
       (candidate_type bez determinovaného signálu), počítá se jako
       NEJNIŽŠÍ priorita té dvojice, nikdy ne jako "dostupné") ->
       nadmořská výška sestupně (vyšší bod = typicky lepší výhled,
       stejný předpoklad jako PŮVODNÍ skript) -> `id` (čistě pro stabilní
       determinismus při úplné shodě všeho ostatního).
    3. Skupiny se pak PROKLÁDAJÍ round-robin (jeden kandidát z každé
       skupiny, pak další kolo) - zadání bod 3 "postupně zpracovávat
       různá významná území ČR, ne jen okolí jednoho města". Skupiny
       samy jsou řazené podle jména (deterministické, `grid_00_00`
       před `grid_00_01`...), takže výsledné pořadí je 100%
       reprodukovatelné nad stejnými daty."""
    groups: dict[str, list[dict]] = {}
    for c in candidates:
        key = c.get("region") or "_unknown"
        groups.setdefault(key, []).append(c)

    def _within_group_key(c: dict) -> tuple:
        is_priority = c.get("candidate_type") in _PRIORITY_TYPES
        near_city = c.get("near_city") is True
        elevation = c.get("elevation_m")
        return (
            0 if is_priority else 1,
            0 if near_city else 1,
            -(elevation if elevation is not None else -1.0),
            c["id"],
        )

    ordered_groups = [sorted(groups[k], key=_within_group_key) for k in sorted(groups)]

    interleaved: list[dict] = []
    for round_ in zip_longest(*ordered_groups, fillvalue=None):
        for c in round_:
            if c is not None:
                interleaved.append(c)
    return interleaved


def _candidate_pool(args) -> list[dict]:
    """Vybere VSTUPNÍ množinu kandidátů PŘED stavovým/typovým filtrem
    (zadání bod 2 - "výběr čistě nad existujícími daty/rozhraními").
    Tři vzájemně se vylučující režimy, přesně jedna geografická volba
    na běh (zadání nejmenuje kombinaci, a kombinovat "region" bbox s
    "near" radiusem by jen zbytečně komplikovalo CLI bez reálné potřeby)."""
    if args.near_lat is not None:
        # Milestone "Nationwide Live Search" infrastruktura - STEJNÁ
        # indexovaná SQL funkce jako `/api/search`, žádný duplicitní
        # geo-filtr kód.
        return catalog_db.radius_search(args.near_lat, args.near_lon, args.near_radius_km)
    catalog = load_candidate_catalog(use_cache=False)
    if args.region:
        bbox = REGIONS[args.region].bbox
        return [c for c in catalog if _in_bbox(c, bbox)]
    return catalog


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--limit", type=int, default=None, help="Maximální počet kandidátů k předpočítání v tomhle běhu (výchozí: bez limitu - POZOR u celostátního katalogu).")
    parser.add_argument("--region", type=str, default=None, choices=list(REGIONS), help="Omezit na bbox jednoho z původních 3 regionů (beskydy/krkonose/vsetinsko). Vzájemně se vylučuje s --near-lat.")
    parser.add_argument("--near-lat", type=float, default=None, help="Střed geografické oblasti zájmu (spolu s --near-lon/--near-radius-km). Vzájemně se vylučuje s --region.")
    parser.add_argument("--near-lon", type=float, default=None, help="Viz --near-lat.")
    parser.add_argument("--near-radius-km", type=float, default=25.0, help="Poloměr oblasti zájmu v km (výchozí 25, stejně jako /api/search). Použije se jen spolu s --near-lat/--near-lon.")
    parser.add_argument("--candidate-type", type=str, default=None, help="Omezit jen na jeden typ (peak/hill/viewpoint/tower/castle).")
    parser.add_argument("--force", action="store_true", help="Přepočítat i kandidáty, kteří už mají 'ready' cache (jinak se přeskočí). 'running' se přeskakuje VŽDY, viz select_candidates().")
    parser.add_argument("--dry-run", action="store_true", help="Jen ukázat, KOLIK a KTEŘÍ kandidáti by se zpracovali (rozpad podle oblasti/typu) - nic nepočítá, nic nezapisuje do cache.")
    args = parser.parse_args()

    if args.near_lat is not None and args.region is not None:
        print("--region a --near-lat/--near-lon se vzájemně vylučují - zvol jeden geografický filtr.")
        return
    if args.near_lat is not None and args.near_lon is None:
        print("--near-lat vyžaduje i --near-lon.")
        return

    terrain_cache.init_db()
    cache_key = terrain_cache.compute_cache_key()
    print(f"Cache key (engine v{terrain_cache.TERRAIN_ENGINE_VERSION}, schema v{terrain_cache.TERRAIN_CACHE_SCHEMA_VERSION}): {cache_key}")

    pool = _candidate_pool(args)
    if not pool:
        print("Vybraná oblast/katalog neobsahuje žádné kandidáty - spusť nejdřív scripts/build_national_catalog.py, nebo zkontroluj --region/--near-lat.")
        return

    to_process, skipped_ready, skipped_running = select_candidates(pool, cache_key, args.candidate_type, args.force)
    ordered = prioritize_candidates(to_process)
    if args.limit is not None:
        ordered = ordered[: args.limit]

    area_desc = (
        f"okolí ({args.near_lat:.4f}, {args.near_lon:.4f}), radius {args.near_radius_km:g} km" if args.near_lat is not None
        else f"region '{args.region}'" if args.region
        else "celostátní katalog"
    )
    by_region = {}
    by_type = {}
    for c in ordered:
        by_region[c.get("region") or "_unknown"] = by_region.get(c.get("region") or "_unknown", 0) + 1
        by_type[c.get("candidate_type")] = by_type.get(c.get("candidate_type"), 0) + 1

    print(f"Oblast: {area_desc}. Kandidátů v oblasti po typovém filtru: {len(pool) if not args.candidate_type else sum(1 for c in pool if c.get('candidate_type') == args.candidate_type)}.")
    print(f"Přeskočeno (už 'ready', bez --force): {skipped_ready}. Přeskočeno (právě 'running' jinde): {skipped_running}.")
    print(f"K zpracování v tomhle běhu (po prioritizaci{' + limitu' if args.limit is not None else ''}): {len(ordered)}.")
    print(f"  podle mřížkové oblasti (region): {dict(sorted(by_region.items()))}")
    print(f"  podle typu: {dict(sorted(by_type.items()))}")

    if args.dry_run:
        print("\n--dry-run: nic se nepočítá ani nezapisuje. Pořadí prvních kandidátů, které by se zpracovaly:")
        for i, c in enumerate(ordered[:20], start=1):
            label = c.get("display_name") or c.get("name") or c["id"]
            print(f"  {i:3d}. {label:35s} type={c.get('candidate_type'):10s} region={c.get('region')!s:12s} near_city={c.get('near_city')!s:5s} elevation={c.get('elevation_m')}")
        if len(ordered) > 20:
            print(f"  ... a dalších {len(ordered) - 20}.")
        return

    succeeded = 0
    failed = 0
    total_time = 0.0
    for i, c in enumerate(ordered, start=1):
        label = c.get("display_name") or c.get("name") or c["id"]
        t0 = time.time()
        try:
            static = compute_static_terrain(c["lat"], c["lon"])
        except Exception as exc:  # noqa: BLE001 - jeden špatný kandidát nesmí zastavit dávku (stejný vzor jako candidate_batch.py)
            dt = time.time() - t0
            terrain_cache.save_failed(c["id"], cache_key, c["lat"], c["lon"], str(exc))
            print(f"  [{i}/{len(ordered)}] {label:35s} {dt:6.1f}s  FAILED: {exc}")
            failed += 1
            total_time += dt
            continue
        dt = time.time() - t0
        written = terrain_cache.save_ready(
            c["id"], cache_key, c["lat"], c["lon"], static, dt, region=c.get("region"), candidate_type=c.get("candidate_type")
        )
        res_note = f", DEM {static.dem.resolution_m:.1f}m/px" if static.dem.resolution_m > DEFAULT_PIXEL_SIZE_M * 1.001 else ""
        if written:
            print(f"  [{i}/{len(ordered)}] {label:35s} {dt:6.1f}s  ok (elevation {static.location.elevation_m:.0f}m{res_note})")
        else:
            print(f"  [{i}/{len(ordered)}] {label:35s} {dt:6.1f}s  ok, ale horší než existující záznam - ZACHOVÁN PŮVODNÍ (nový zahozen{res_note})")
        succeeded += 1
        total_time += dt

    print(f"\nHotovo: {succeeded} ok, {failed} failed, {total_time:.1f}s celkem" + (f" ({total_time/len(ordered):.1f}s/kandidát)" if ordered else ""))
    counts = terrain_cache.count_by_status(cache_key=cache_key)
    print(f"Stav cache (pro aktuální cache_key {cache_key}): {counts}")


if __name__ == "__main__":
    main()
