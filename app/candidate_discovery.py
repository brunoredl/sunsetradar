"""Fáze 8 - Candidate Discovery (zadání bod 9/10): PRVNÍ krok směrem k
"Find the best sunset in Czechia" - objevení kandidátních míst (vrcholy,
kopce, vyhlídky) z veřejných geografických dat. Tenhle modul POUZE
objevuje a filtruje kandidáty - NIC nepočítá o terénu/slunci (to zůstává
výhradně `analyze_location()`/`compute_sunset_rating()`, viz
`app/candidate_batch.py`).

ZDROJ (zadání bod 8/10, audit před implementací): OpenStreetMap přes
Overpass API - veřejné, bez API klíče, žádný účet. Licence dat: ODbL
(OpenStreetMap contributors) - vhodné pro veřejnou aplikaci s
atribucí, stejně jako u map vrstev (viz README). Preferováno před
placenými/proprietárními POI databázemi přesně podle zadání bodu 8.

AUDIT NÁLEZ: hlavní veřejná instance `overpass-api.de` z tohohle
vývojového prostředí trvale vracela HTTP 406 (síťové prostředí/edge
proxy mezi tímhle sandboxem a serverem, ne problém dotazu samotného -
ověřeno stejný dotaz přes `requests` i `curl`, s různými hlavičkami).
Oficiální francouzské zrcadlo `overpass.openstreetmap.fr` (běžně
doporučované na wiki.openstreetmap.org/wiki/Overpass_API#Public_Overpass_API_instances)
funguje spolehlivě - endpoint je nastavitelný, aby šlo v produkčním
nasazení přepnout na jinou instanci/vlastní server podle potřeby."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import requests

OVERPASS_ENDPOINT = "https://overpass.openstreetmap.fr/api/interpreter"
DEFAULT_TIMEOUT_S = 40

# Hrubý obalový obdélník České republiky (south, west, north, east) -
# pro filtraci "je kandidát vůbec v ČR" (zadání bod 12).
CZECHIA_BBOX = (48.5, 12.0, 51.1, 19.0)

# OSM tagy použité pro objevování kandidátů (zadání bod 9) - jen typy
# relevantní pro pozorování západu Slunce, ŽÁDNÉ komerční POI. Každá
# položka je skupina (key, value) párů, které se v Overpass dotazu
# spojí AND-em na jeden `node[...]​[...]` selektor - rozhledny (Fáze
# 11 audit) potřebují DVA tagy najednou (`man_made=tower` +
# `tower:type=observation`), samotné `man_made=tower` by zachytilo i
# vysílače/vodojemy apod.
DEFAULT_TAGS: tuple[tuple[tuple[str, str], ...], ...] = (
    (("natural", "peak"),),
    (("natural", "hill"),),
    (("tourism", "viewpoint"),),
    (("man_made", "tower"), ("tower:type", "observation")),
)

_TYPE_LABELS = {"peak": "Vrchol", "hill": "Kopec", "viewpoint": "Vyhlídka", "tower": "Rozhledna"}


class CandidateDiscoveryError(RuntimeError):
    """Overpass dotaz selhal (síť/timeout/rate limit) - NIKDY tichý
    prázdný výsledek, aby se to nepletlo s "opravdu žádní kandidáti"."""


@dataclass
class Candidate:
    """Jedno kandidátní místo z discovery - ŽÁDNÁ terrain/solar data,
    jen identita a poloha (viz docstring modulu)."""

    id: str
    name: str
    lat: float
    lon: float
    candidate_type: str  # "peak" | "hill" | "viewpoint" | "tower"
    elevation_m: float | None  # z OSM tagu `ele`, pokud existuje - jinak None (nevymýšlet)
    source: str = "openstreetmap"
    metadata: dict[str, Any] = field(default_factory=dict)


def _build_overpass_query(
    bbox: tuple[float, float, float, float], tags: tuple[tuple[tuple[str, str], ...], ...]
) -> str:
    south, west, north, east = bbox
    lines = []
    for tag_group in tags:
        selectors = "".join(f'["{k}"="{v}"]' for k, v in tag_group)
        lines.append(f"  node{selectors}({south},{west},{north},{east});")
    return f"[out:json][timeout:{DEFAULT_TIMEOUT_S}];\n(\n" + "\n".join(lines) + "\n);\nout;"


def discover_candidates(
    bbox: tuple[float, float, float, float],
    tags: tuple[tuple[tuple[str, str], ...], ...] = DEFAULT_TAGS,
    endpoint: str = OVERPASS_ENDPOINT,
    session: Any = None,
) -> list[Candidate]:
    """Dotáže Overpass API pro danou oblast (jeden request pro celou
    oblast najednou, ne per-kandidát - zadání bod 10 "nedělej request
    pro každou lokaci zvlášť"). Vrací syrové kandidáty BEZ filtrace -
    volající použije `deduplicate()`/`filter_within_czechia()` apod."""
    query = _build_overpass_query(bbox, tags)
    http = session or requests
    try:
        response = http.post(endpoint, data={"data": query}, timeout=DEFAULT_TIMEOUT_S)
        response.raise_for_status()
    except requests.exceptions.RequestException as exc:
        raise CandidateDiscoveryError(f"Overpass dotaz selhal: {exc}") from exc
    return parse_overpass_response(response.json())


def parse_overpass_response(data: dict[str, Any]) -> list[Candidate]:
    """Čistá parsovací funkce (testovatelná bez sítě - viz
    tests/test_candidate_discovery.py). Přeskočí prvky, které nejsou
    `node` (way/relation by potřebovaly `out center`, mimo rozsah
    prototypu), prvky bez souřadnic, a tagy mimo `DEFAULT_TYPES`."""
    candidates: list[Candidate] = []
    for element in data.get("elements", []):
        if element.get("type") != "node":
            continue
        lat, lon = element.get("lat"), element.get("lon")
        if lat is None or lon is None:
            continue
        tags = element.get("tags", {}) or {}
        candidate_type = _infer_type(tags)
        if candidate_type is None:
            continue
        osm_id = element.get("id")
        if osm_id is None:
            continue
        name = tags.get("name") or _fallback_name(candidate_type, lat, lon)
        candidates.append(
            Candidate(
                id=f"node/{osm_id}",
                name=name,
                lat=float(lat),
                lon=float(lon),
                candidate_type=candidate_type,
                elevation_m=_parse_elevation(tags.get("ele")),
                metadata={"osm_tags": tags},
            )
        )
    return candidates


def _infer_type(tags: dict[str, Any]) -> str | None:
    if tags.get("natural") == "peak":
        return "peak"
    if tags.get("natural") == "hill":
        return "hill"
    # Rozhledna (`man_made=tower` + `tower:type=observation`) je
    # specifičtější než generické `tourism=viewpoint` - hodně rozhleden
    # má OBA tagy zároveň (ověřeno auditem, Fáze 11), takže "tower" má
    # přednost, aby se zobrazovaly jako vlastní typ, ne jako obyčejná
    # vyhlídka.
    if tags.get("man_made") == "tower" and tags.get("tower:type") == "observation":
        return "tower"
    if tags.get("tourism") == "viewpoint":
        return "viewpoint"
    return None


def _fallback_name(candidate_type: str, lat: float, lon: float) -> str:
    label = _TYPE_LABELS.get(candidate_type, "Místo")
    return f"{label} {lat:.3f}°N {lon:.3f}°E"


def _parse_elevation(raw: Any) -> float | None:
    if raw is None:
        return None
    try:
        return float(str(raw).replace(",", "."))
    except ValueError:
        return None


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def deduplicate(candidates: list[Candidate], min_distance_m: float = 150.0) -> list[Candidate]:
    """Odstraní kandidáty blíž než `min_distance_m` k už přijatému
    kandidátovi (zadání bod 12 - "odstraň duplicity"). Prostý O(n²) -
    dost rychlé pro desítky až stovky bodů z jedné oblasti; při
    škálování na celou ČR by šlo nahradit prostorovým indexem."""
    accepted: list[Candidate] = []
    for candidate in candidates:
        if any(haversine_m(candidate.lat, candidate.lon, a.lat, a.lon) < min_distance_m for a in accepted):
            continue
        accepted.append(candidate)
    return accepted


def filter_within_bbox(candidates: list[Candidate], bbox: tuple[float, float, float, float] = CZECHIA_BBOX) -> list[Candidate]:
    """Odstraní kandidáty mimo zadaný obalový obdélník (zadání bod 12 -
    "body mimo ČR")."""
    south, west, north, east = bbox
    return [c for c in candidates if south <= c.lat <= north and west <= c.lon <= east]


DEFAULT_CACHE_DIR = Path(__file__).resolve().parent.parent / "data" / "overpass_cache"


def discover_candidates_cached(
    region: str,
    bbox: tuple[float, float, float, float],
    tags: tuple[tuple[tuple[str, str], ...], ...] = DEFAULT_TAGS,
    cache_dir: Path = DEFAULT_CACHE_DIR,
    endpoint: str = OVERPASS_ENDPOINT,
) -> list[Candidate]:
    """Stejné jako `discover_candidates()`, ale s PERZISTENTNÍ cache
    syrové Overpass odpovědi na disk (zadání bod 10 - "stahuj data po
    regionech, nedělej request pro každou lokaci zvlášť"). Existuje-li
    `<cache_dir>/<region>_combined.json`, použije se rovnou (žádný
    síťový požadavek); jinak se zavolá Overpass a odpověď se uloží pro
    příště.

    AUDIT NÁLEZ (Fáze 8): veřejné Overpass instance (overpass-api.de,
    overpass.kumi.systems, overpass.openstreetmap.fr) se z tohohle
    vývojového prostředí postupně staly nedostupné (406 / 504 / 403
    "only available to white-listed usages") po několika dotazech
    během vývoje - proto je cache nutná i pro první běh, ne jen jako
    optimalizace. Data pro Beskydy v `data/overpass_cache/beskydy_combined.json`
    jsou SKUTEČNÁ data stažená z overpass.openstreetmap.fr (ne vymyšlená),
    jen uložená předem kvůli téhle dostupnostní bariéře - viz PLAN.md."""
    cache_path = cache_dir / f"{region}_combined.json"
    if cache_path.exists():
        raw = json.loads(cache_path.read_text())
    else:
        query = _build_overpass_query(bbox, tags)
        try:
            response = requests.post(endpoint, data={"data": query}, timeout=DEFAULT_TIMEOUT_S)
            response.raise_for_status()
        except requests.exceptions.RequestException as exc:
            raise CandidateDiscoveryError(
                f"Overpass dotaz selhal a pro region '{region}' není žádná cache: {exc}"
            ) from exc
        raw = response.json()
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(raw, ensure_ascii=False, indent=1))
    return parse_overpass_response(raw)
