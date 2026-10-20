"""Milestone "Real Candidate Catalog + Spatial Search" - CENTRÁLNÍ
katalog VŠECH doposud objevených kandidátů napříč regiony, ne jen té
malé (12-13 na region) ručně analyzované podmnožiny v
`data/candidates_<region>.json`.

AUDIT NÁLEZ (před implementací): syrová Overpass data pro každý region
už existují na disku (`data/overpass_cache/<region>_combined.json`,
viz `discover_candidates_cached()`), jen nebyla nikdy sloučená do
jednoho dotazovatelného celku - `/api/search` proto uměl hledat jen v
tom malém předanalyzovaném vzorku. Tenhle modul NEDĚLÁ nový scraping,
jen sloučí a deduplikuje data, která už jsou stažená.

`build_catalog()`/`data/candidate_catalog.json` (JSON, 3 ručně vybrané
regiony) je PŮVODNÍ, dnes už jen ZPĚTNĚ KOMPATIBILNÍ cesta - milestone
"Data Foundation" nahradil zdroj pravdy celostátním importem do SQLite
(`app/catalog_db.py`, `scripts/build_national_catalog.py`), přesně
podle důvodu, který si tenhle modul sám předjímal: "Kdyby katalog
jednou narostl na desítky tisíc záznamů (celá ČR), SQLite s prostorovým
indexem by dávalo smysl." `load_candidate_catalog()` teď čte PŘEDNOSTNĚ
SQLite (pokud existuje a má data) a jen když ne, spadne zpátky na
starý JSON soubor - beze změny návratového typu (pořád `list[dict]`),
takže žádný volající kód (app/main.py, testy) se nemusí měnit."""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from app import catalog_db
from app.candidate_discovery import Candidate, deduplicate, discover_candidates_cached, filter_within_bbox
from app.environment_context import classify_candidate_environment, discover_environment_features_cached
from app.regions import REGIONS, Region
from app.slugs import assign_slugs

DEFAULT_CATALOG_PATH = Path(__file__).resolve().parent.parent / "data" / "candidate_catalog.json"

# Zeměpisný obalový obdélník celé ČR (stejný jako
# app/candidate_discovery.py::CZECHIA_BBOX) - i katalog složený z
# regionálních dotazů si pojistí, že žádný bod mimo ČR neproklouzne.
CZECHIA_BBOX = (48.5, 12.0, 51.1, 19.0)


def build_catalog(regions: dict[str, Region] | None = None) -> dict:
    """Sloučí kandidáty ze VŠECH registrovaných regionů do jednoho
    katalogu. Čte (nebo v případě cache-miss dotáže) syrová Overpass
    data přes STÁVAJÍCÍ `discover_candidates_cached()` - žádný nový
    scraper. Deduplikace probíhá GLOBÁLNĚ přes celý sloučený seznam
    (ne jen uvnitř jednoho regionu), protože sousední regiony mohou
    mít bbox hrany, které se dotýkají (Beskydy west=18.3 ==
    Vsetínsko east=18.3) - bez globální deduplikace by se hraniční
    bod mohl objevit v katalogu dvakrát."""
    regions = regions if regions is not None else REGIONS
    merged: list[Candidate] = []
    per_region_raw_count: dict[str, int] = {}
    for key, region in regions.items():
        raw = discover_candidates_cached(key, region.bbox)
        per_region_raw_count[key] = len(raw)
        for c in raw:
            c.metadata = {**c.metadata, "region": key}
            merged.append(c)

    filtered = filter_within_bbox(merged, CZECHIA_BBOX)
    deduped = deduplicate(filtered)

    # Milestone "Complete Product Build" (pokračování) - REÁLNÝ Water/
    # Forest/City signál (zadání bod 5 - "žádné fake labels"), počítaný
    # JEDNOU při stavbě katalogu (statický, stejně jako candidate
    # samotný - nemění se podle data). Environment features se natahují
    # jen jednou na region (cache na disku, stejný vzor jako discovery výše).
    environment_by_region = {
        key: discover_environment_features_cached(key, region.bbox) for key, region in regions.items()
    }

    candidates_json = []
    for c in deduped:
        d = asdict(c)
        region_key = c.metadata.get("region")
        d["region"] = region_key
        features = environment_by_region.get(region_key)
        if features is not None:
            env = classify_candidate_environment(c.lat, c.lon, features)
            d["near_water"] = env.near_water
            d["near_forest"] = env.near_forest
            d["near_city"] = env.near_city
            d["nearest_water_km"] = env.nearest_water_km
            d["nearest_forest_km"] = env.nearest_forest_km
            d["nearest_city_km"] = env.nearest_city_km
        candidates_json.append(d)

    # Milestone "Complete Product Build" (pokračování) - stabilní slug
    # pro `/sunset/{slug}/` (zadání bod 2), počítaný JEDNOU při stavbě
    # katalogu (stejně jako environment signál výše).
    assign_slugs(candidates_json)

    return {
        "generated_at": None,  # nastaví volající (build_candidate_catalog.py) - čistá funkce tady nezná "teď"
        "source": "OpenStreetMap via Overpass API",
        "regions": {key: {"name": r.name, "bbox": list(r.bbox)} for key, r in regions.items()},
        "raw_count_per_region": per_region_raw_count,
        "candidate_count": len(candidates_json),
        "candidates": candidates_json,
    }


_catalog_cache: list[dict] | None = None


def load_candidate_catalog(
    path: Path = DEFAULT_CATALOG_PATH,
    use_cache: bool = True,
    db_path: Path = catalog_db.DEFAULT_DB_PATH,
) -> list[dict]:
    """Načte centrální katalog (zadání bod 5 - `load_candidate_catalog()`).
    Milestone "Data Foundation" - SQLite (`db_path`) je PŘEDNOSTNÍ zdroj,
    pokud existuje a má aspoň jeden řádek; jinak zpětně kompatibilní
    fallback na starý JSON (`path`). Modulová in-memory cache (jednoduchý
    seznam, žádná další vrstva) - katalog je řádově jednotky MB i pro
    celé ČR, čte se jednou za běh procesu, ne při každém requestu
    (lineární sken v paměti je pro tuhle velikost v pořádku, viz
    PLAN.md benchmark)."""
    global _catalog_cache
    if use_cache and _catalog_cache is not None:
        return _catalog_cache

    if db_path.exists():
        candidates = catalog_db.list_all(db_path)
        if candidates:
            if use_cache:
                _catalog_cache = candidates
            return candidates

    if not path.exists():
        return []
    data = json.loads(path.read_text())
    candidates = data.get("candidates", [])
    if use_cache:
        _catalog_cache = candidates
    return candidates


def invalidate_catalog_cache() -> None:
    """Pro testy / po přegenerování katalogu za běhu procesu."""
    global _catalog_cache
    _catalog_cache = None
