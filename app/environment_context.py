"""Milestone "Complete Product Build" (pokračování) - REÁLNÝ datový
signál pro Water/Forest/City personalizaci (dřív `disabled`, protože
candidate katalog neměl žádnou environmentální metadatu).

STEJNÝ zdroj/vzor jako `app/candidate_discovery.py` (Overpass API,
persistentní disková cache syrové odpovědi) - "nechci kompletní OSM
databázi, stačí rozumný spatial lookup" (zadání): JEDEN Overpass dotaz
na region pro všechny tři kategorie najednou, pak haversine vzdálenost
od každého kandidáta k NEJBLIŽŠÍMU prvku každé kategorie.

ŽÁDNÝ shapely/přesná polygon geometrie (není v projektu závislost a
pro "je poblíž voda/les/město" by to byla zbytečná komplexita) - ways/
relations se berou přes Overpass `out center` (jejich reprezentativní
bod), což je pro účel "kolik km je to k nejbližšímu jezeru/lesu/městu"
dostatečně přesné a naprosto transparentní o svém omezení."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests

from app.candidate_discovery import haversine_m

OVERPASS_ENDPOINT = "https://overpass.openstreetmap.fr/api/interpreter"
DEFAULT_TIMEOUT_S = 40
DEFAULT_CACHE_DIR = Path(__file__).resolve().parent.parent / "data" / "overpass_cache"

# Prahy "je to poblíž" (zadání bod 5 - "rozumný spatial lookup", ne
# kalibrovaný model) - voda/les typicky menší plochy blízko kandidáta
# už znamenají "výhled zahrnuje", město potřebuje větší dosah, protože
# jde o CELÉ sídlo, ne bod přesně pod kandidátem.
NEAR_WATER_KM = 2.0
NEAR_FOREST_KM = 1.5
NEAR_CITY_KM = 6.0

_QUERY_TEMPLATE = """[out:json][timeout:{timeout}];
(
  node["natural"="water"]({south},{west},{north},{east});
  way["natural"="water"]({south},{west},{north},{east});
  way["waterway"="river"]({south},{west},{north},{east});
  way["natural"="wood"]({south},{west},{north},{east});
  way["landuse"="forest"]({south},{west},{north},{east});
  node["place"~"^(city|town)$"]({south},{west},{north},{east});
);
out center;"""


@dataclass
class EnvironmentFeatures:
    water_points: list[tuple[float, float]]
    forest_points: list[tuple[float, float]]
    city_points: list[tuple[float, float]]


def _element_point(element: dict[str, Any]) -> tuple[float, float] | None:
    if "lat" in element and "lon" in element:
        return element["lat"], element["lon"]
    center = element.get("center")
    if center:
        return center["lat"], center["lon"]
    return None


def parse_environment_response(data: dict[str, Any]) -> EnvironmentFeatures:
    """Čistá parsovací funkce (testovatelná bez sítě, stejný vzor jako
    `candidate_discovery.parse_overpass_response()`)."""
    water, forest, city = [], [], []
    for element in data.get("elements", []):
        tags = element.get("tags", {}) or {}
        point = _element_point(element)
        if point is None:
            continue
        if tags.get("natural") == "water" or tags.get("waterway") == "river":
            water.append(point)
        elif tags.get("natural") == "wood" or tags.get("landuse") == "forest":
            forest.append(point)
        elif tags.get("place") in ("city", "town"):
            city.append(point)
    return EnvironmentFeatures(water_points=water, forest_points=forest, city_points=city)


def discover_environment_features_cached(
    region: str,
    bbox: tuple[float, float, float, float],
    cache_dir: Path = DEFAULT_CACHE_DIR,
    endpoint: str = OVERPASS_ENDPOINT,
) -> EnvironmentFeatures:
    """Stejný cache vzor jako `candidate_discovery.discover_candidates_cached()` -
    `<cache_dir>/<region>_environment.json` na disku, žádný nový
    síťový požadavek při opakovaném běhu."""
    cache_path = cache_dir / f"{region}_environment.json"
    if cache_path.exists():
        raw = json.loads(cache_path.read_text())
    else:
        south, west, north, east = bbox
        query = _QUERY_TEMPLATE.format(timeout=DEFAULT_TIMEOUT_S, south=south, west=west, north=north, east=east)
        response = requests.post(endpoint, data={"data": query}, timeout=DEFAULT_TIMEOUT_S + 10)
        response.raise_for_status()
        raw = response.json()
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(raw, ensure_ascii=False, indent=1))
    return parse_environment_response(raw)


def _nearest_km(lat: float, lon: float, points: list[tuple[float, float]]) -> float | None:
    if not points:
        return None
    return min(haversine_m(lat, lon, p_lat, p_lon) for p_lat, p_lon in points) / 1000.0


@dataclass
class EnvironmentClassification:
    near_water: bool
    near_forest: bool
    near_city: bool
    nearest_water_km: float | None
    nearest_forest_km: float | None
    nearest_city_km: float | None


def classify_candidate_environment(lat: float, lon: float, features: EnvironmentFeatures) -> EnvironmentClassification:
    """Poctivé o nejistotě (zadání bod 6/32): `near_*` je `False`, pokud
    žádný prvek dané kategorie v regionu vůbec nebyl nalezen (ne proto,
    že by NUTNĚ žádný neexistoval - jen v tomhle regionálním Overpass
    dotazu) - `nearest_*_km` je `None` v tom případě, ne fake 0/nekonečno."""
    water_km = _nearest_km(lat, lon, features.water_points)
    forest_km = _nearest_km(lat, lon, features.forest_points)
    city_km = _nearest_km(lat, lon, features.city_points)
    return EnvironmentClassification(
        near_water=water_km is not None and water_km <= NEAR_WATER_KM,
        near_forest=forest_km is not None and forest_km <= NEAR_FOREST_KM,
        near_city=city_km is not None and city_km <= NEAR_CITY_KM,
        nearest_water_km=round(water_km, 1) if water_km is not None else None,
        nearest_forest_km=round(forest_km, 1) if forest_km is not None else None,
        nearest_city_km=round(city_km, 1) if city_km is not None else None,
    )
