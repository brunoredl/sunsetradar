"""Milestone "Data Foundation" - celostátní discovery pro sunset
kandidáty + gazetteer pojmenovaných sídel (zadání bod 3/5). Nad
mřížkou z `app/national_grid.py`, JEDEN Overpass dotaz na dlaždici pro
OBOJÍ najednou (kandidáti i sídla pro name-resolution) - stejný úsporný
vzor jako `app/environment_context.py` ("jeden dotaz na oblast pro
všechny kategorie najednou").

ODOLNOST (zadání bod 5 - "retry při transient errors, pokračuj po
failed requestu"): víc veřejných Overpass zrcadel se zkouší popořadě,
s reálným zpožděním mezi pokusy (fair use, stejná zkušenost s
rate-limitingem jako u `app/environment_context.py`/
`app/candidate_discovery.py` v týhle session) - vyčerpání všech
pokusů/zrcadel na jedné dlaždici NESHODÍ celý import, jen se ta
dlaždice zaznamená jako `failed` a import pokračuje dál (stejná
filozofie jako `app/terrain_worker.py`)."""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import requests

from app.candidate_discovery import Candidate, CZECHIA_BBOX, deduplicate, filter_within_bbox
from app.environment_context import classify_candidate_environment
from app.place_names import (
    MUNICIPALITY_SEARCH_KM,
    NEARBY_LOCALITY_KM,
    PlaceIndex,
    resolve_display_name,
    resolve_nearby_places,
)

logger = logging.getLogger(__name__)

# Víc veřejných Overpass zrcadel (STEJNÉ API, ne nový poskytovatel) -
# `overpass.openstreetmap.fr` je v týhle session ověřeně nejspolehlivější,
# ostatní dva jako fallback při rate-limitu/výpadku (zadání bod 5).
DEFAULT_ENDPOINTS: tuple[str, ...] = (
    "https://overpass.openstreetmap.fr/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass-api.de/api/interpreter",
)
DEFAULT_TIMEOUT_S = 90
DEFAULT_CACHE_DIR = Path(__file__).resolve().parent.parent / "data" / "overpass_cache" / "national_grid"

# Candidate typy (zadání bod 4) - "lookout"/"other elevated scenic
# location" NEJSOU vlastní OSM tagy s jasným, jednoznačným významem
# (na rozdíl od peak/hill/viewpoint/tower/castle) - reálně se v OSM
# datech promítají do "viewpoint" nebo "tower" (rozhledna = přesně
# "lookout"), takže se nepřidává další umělá kategorie jen kvůli
# jménu - poctivě zdokumentováno tady, ne tiše vynecháno.
_CANDIDATE_QUERY_LINES = (
    'node["natural"="peak"]({s},{w},{n},{e});',
    'node["natural"="hill"]({s},{w},{n},{e});',
    'node["tourism"="viewpoint"]({s},{w},{n},{e});',
    'node["man_made"="tower"]["tower:type"="observation"]({s},{w},{n},{e});',
    'node["historic"="castle"]({s},{w},{n},{e});',
    'way["historic"="castle"]({s},{w},{n},{e});',
)
# Sídla pro name-resolution (zadání bod 2) - "municipality" tier
# (city/town/village, zhruba odpovídá české "obci") a "locality" tier
# (hamlet/suburb/quarter/isolated_dwelling, menší/specifičtější
# pojmenovaná místa) - obojí v JEDNOM regexu, rozliší se až při parsování.
_PLACE_QUERY_LINE = 'node["place"~"^(city|town|village|hamlet|suburb|quarter|isolated_dwelling)$"]({s},{w},{n},{e});'

MUNICIPALITY_PLACE_TAGS = {"city", "town", "village"}
LOCALITY_PLACE_TAGS = {"hamlet", "suburb", "quarter", "isolated_dwelling"}


class NationalDiscoveryError(RuntimeError):
    """Všechna zrcadla/pokusy pro danou dlaždici selhaly."""


@dataclass
class PlaceRecord:
    name: str
    lat: float
    lon: float
    tier: str  # "municipality" | "locality"


@dataclass
class GridCellResult:
    candidates: list[Candidate]
    places: list[PlaceRecord]


def _build_query(bbox: tuple[float, float, float, float], timeout: int) -> str:
    south, west, north, east = bbox
    lines = [line.format(s=south, w=west, n=north, e=east) for line in _CANDIDATE_QUERY_LINES]
    lines.append(_PLACE_QUERY_LINE.format(s=south, w=west, n=north, e=east))
    body = "\n  ".join(lines)
    return f"[out:json][timeout:{timeout}];\n(\n  {body}\n);\nout center;"


def _element_point(element: dict[str, Any]) -> tuple[float, float] | None:
    if "lat" in element and "lon" in element:
        return element["lat"], element["lon"]
    center = element.get("center")
    if center:
        return center["lat"], center["lon"]
    return None


def _parse_elevation(raw: Any) -> float | None:
    if raw is None:
        return None
    try:
        return float(str(raw).replace(",", "."))
    except ValueError:
        return None


def _infer_candidate_type(tags: dict[str, Any]) -> str | None:
    if tags.get("natural") == "peak":
        return "peak"
    if tags.get("natural") == "hill":
        return "hill"
    if tags.get("man_made") == "tower" and tags.get("tower:type") == "observation":
        return "tower"
    if tags.get("tourism") == "viewpoint":
        return "viewpoint"
    if tags.get("historic") == "castle":
        return "castle"
    return None


def parse_grid_cell_response(data: dict[str, Any], cell_key: str) -> GridCellResult:
    """Čistá parsovací funkce (testovatelná bez sítě - stejný vzor jako
    `app.candidate_discovery.parse_overpass_response()`). Kandidát bez
    `name` tagu dostane `name=None` (NE vymyšlený placeholder - zadání
    bod 1, name resolution na tohle navazuje v `app/place_names.py`)."""
    candidates: list[Candidate] = []
    places: list[PlaceRecord] = []
    for element in data.get("elements", []):
        point = _element_point(element)
        if point is None:
            continue
        lat, lon = point
        tags = element.get("tags", {}) or {}
        el_type = element.get("type")
        el_id = element.get("id")
        if el_id is None:
            continue

        place_tag = tags.get("place")
        if place_tag in MUNICIPALITY_PLACE_TAGS or place_tag in LOCALITY_PLACE_TAGS:
            name = tags.get("name")
            if name:
                tier = "municipality" if place_tag in MUNICIPALITY_PLACE_TAGS else "locality"
                places.append(PlaceRecord(name=name, lat=float(lat), lon=float(lon), tier=tier))
            continue

        candidate_type = _infer_candidate_type(tags)
        if candidate_type is None:
            continue
        candidates.append(
            Candidate(
                id=f"{el_type}/{el_id}",
                name=tags.get("name") or "",  # prázdné - name-resolution pipeline rozhodne display_name
                lat=float(lat),
                lon=float(lon),
                candidate_type=candidate_type,
                elevation_m=_parse_elevation(tags.get("ele")),
                metadata={"osm_tags": tags, "region": cell_key},
            )
        )
    return GridCellResult(candidates=candidates, places=places)


def _fetch_raw(
    bbox: tuple[float, float, float, float],
    endpoints: tuple[str, ...],
    timeout: int,
    max_attempts_per_endpoint: int,
    retry_delay_s: float,
) -> dict[str, Any]:
    query = _build_query(bbox, timeout)
    last_exc: Exception | None = None
    for endpoint in endpoints:
        for attempt in range(1, max_attempts_per_endpoint + 1):
            try:
                response = requests.post(endpoint, data={"data": query}, timeout=timeout + 15)
                response.raise_for_status()
                return response.json()
            except requests.exceptions.RequestException as exc:
                last_exc = exc
                logger.warning(
                    "Overpass dotaz selhal (%s, pokus %d/%d): %s", endpoint, attempt, max_attempts_per_endpoint, exc
                )
                if attempt < max_attempts_per_endpoint:
                    time.sleep(retry_delay_s * attempt)  # rostoucí prodleva (fair use)
    raise NationalDiscoveryError(f"Všechna zrcadla/pokusy selhaly pro bbox {bbox}: {last_exc}")


def build_candidate_rows(
    candidates: list[Candidate],
    places: list[PlaceRecord],
    cell_environment: dict,
) -> tuple[list[dict], int]:
    """Čistá agregační funkce (zadání bod 6/11 - testovatelná deduplikace/
    name-resolution integrace bez sítě) - GLOBÁLNÍ filtr/dedup/name-
    resolution/environment-signál nad kandidáty ze VŠECH dlaždic najednou,
    oddělené od síťového I/O v `scripts/build_national_catalog.py` (ten
    zůstává tenký CLI wrapper). Vrací `(rows, duplicates_removed)` -
    `rows` jsou dicty připravené pro `app.catalog_db.upsert_many()`
    (BEZ slugu - ten se přiřazuje globálně přes `app.slugs.assign_slugs()`
    až volajícím, stejně jako `app/candidate_catalog.py::build_catalog()`)."""
    filtered = filter_within_bbox(candidates, CZECHIA_BBOX)
    deduped = deduplicate(filtered)
    duplicates_removed = len(filtered) - len(deduped)

    # Výkon (živě naměřeno na tomhle milestone - naivní O(kandidáti ×
    # místa) lineární sken trval na celostátním katalogu ~7,9 h): index
    # se postaví JEDNOU, ne pro každého kandidáta znovu, a
    # `resolve_nearby_places()` se volá jen JEDNOU na kandidáta (výsledek
    # se předá do `resolve_display_name()`, aby ho nepočítal podruhé).
    place_index = PlaceIndex(places)

    rows: list[dict] = []
    for c in deduped:
        osm_tags = c.metadata.get("osm_tags", {})
        region_key = c.metadata.get("region")
        resolved_places = resolve_nearby_places(c.lat, c.lon, place_index)
        display_name, name_source = resolve_display_name(
            osm_tags, c.candidate_type, c.lat, c.lon, places, resolved_places=resolved_places
        )

        municipality = (
            resolved_places.municipality.name
            if resolved_places.municipality and resolved_places.municipality.distance_km <= MUNICIPALITY_SEARCH_KM
            else None
        )
        locality = (
            resolved_places.locality.name
            if resolved_places.locality and resolved_places.locality.distance_km <= NEARBY_LOCALITY_KM
            else None
        )

        features = cell_environment.get(region_key)
        env = classify_candidate_environment(c.lat, c.lon, features) if features is not None else None

        rows.append(
            {
                "id": c.id,
                "name": c.name or None,
                "display_name": display_name,
                "name_source": name_source,
                "candidate_type": c.candidate_type,
                "lat": c.lat,
                "lon": c.lon,
                "elevation_m": c.elevation_m,
                "region": region_key,
                "municipality": municipality,
                "locality": locality,
                "cadastral_area": None,
                "source": "openstreetmap",
                "source_id": c.id.split("/", 1)[1] if "/" in c.id else c.id,
                "near_water": env.near_water if env else None,
                "near_forest": env.near_forest if env else None,
                "near_city": env.near_city if env else None,
                "nearest_water_km": env.nearest_water_km if env else None,
                "nearest_forest_km": env.nearest_forest_km if env else None,
                "nearest_city_km": env.nearest_city_km if env else None,
            }
        )
    return rows, duplicates_removed


def discover_grid_cell_cached(
    cell_key: str,
    bbox: tuple[float, float, float, float],
    cache_dir: Path = DEFAULT_CACHE_DIR,
    endpoints: tuple[str, ...] = DEFAULT_ENDPOINTS,
    timeout: int = DEFAULT_TIMEOUT_S,
    max_attempts_per_endpoint: int = 2,
    retry_delay_s: float = 10.0,
) -> GridCellResult:
    """Stejný cache vzor jako zbytek projektu - `<cache_dir>/<cell_key>.json`
    na disku, žádný nový síťový požadavek při opakovaném běhu (zadání
    bod 5 - "jednotlivé requesty cachuj")."""
    cache_path = cache_dir / f"{cell_key}.json"
    if cache_path.exists():
        raw = json.loads(cache_path.read_text())
    else:
        raw = _fetch_raw(bbox, endpoints, timeout, max_attempts_per_endpoint, retry_delay_s)
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(raw, ensure_ascii=False, indent=1))
    return parse_grid_cell_response(raw, cell_key)
