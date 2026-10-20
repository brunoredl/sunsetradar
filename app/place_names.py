"""Milestone "Data Foundation" - deterministická name-resolution
pipeline (zadání bod 1). Nikde se `display_name` nesmí rovnat surovým
GPS souřadnicím - a generovaný název se nikdy neprezentuje jako
oficiální (`name_source` to rozlišuje, viz zadání "nesmí vzniknout
falešný dojem, že se tak místo oficiálně jmenuje").

PRIORITA (zadání bod 1):
  1. "osm"          - name/name:cs/official_name/alt_name tag PŘÍMO na objektu
  2. "local"         - nejbližší pojmenované OSM sídlo "locality" úrovně
                        (hamlet/suburb/quarter/isolated_dwelling) v okruhu NEARBY_LOCALITY_KM
  3. "municipality"  - nejbližší pojmenované OSM sídlo "municipality" úrovně
                        (city/town/village) v okruhu MUNICIPALITY_SEARCH_KM
  4. "cadastral"     - NEIMPLEMENTOVÁNO v tomhle milestone (viz níže) - vždy
                        se přeskočí, pipeline pokračuje na "generated"
  5. "generated"     - deterministicky z (typ, nejbližší známé sídlo) -
                        ŽÁDNÉ AI, ŽÁDNÉ vymýšlení jmen

KATASTRÁLNÍ ÚZEMÍ (zadání bod 2): reálný veřejný zdroj EXISTUJE (ČÚZK
RUIAN GeocodeSOE / https://api-kn.cuzk.gov.cz/), ale vyžaduje přepočet
souřadnic do S-JTSK (EPSG:5514) a ověření kontraktu API - mimo rozsah
tohohle kroku (zadání samo řadí "cadastral" jako nejnižší prioritu,
za "municipality"). `resolve_cadastral_area()` proto vždy vrací `None` -
poctivě nedostupné, ne fake název. Fallback řetězec na tenhle krok
správně reaguje (přeskočí na "generated"), jen zdroj dat není zapojený."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from app.candidate_discovery import haversine_m

# Zadání bod 1 - "rozumný spatial lookup" prahy (stejný duch jako
# app/environment_context.py) - "local" je specifičtější/menší okruh
# než "municipality" (obec může být vzdálenější, ale pořád relevantní
# jako "nad jakým městem to je").
NEARBY_LOCALITY_KM = 3.0
MUNICIPALITY_SEARCH_KM = 20.0

_TYPE_LABEL_CS = {
    "peak": "Vrchol",
    "hill": "Kopec",
    "viewpoint": "Vyhlídka",
    "tower": "Rozhledna",
    "castle": "Hrad",
}

# Tag priorita v rámci "osm" tieru (zadání bod 1 - "specifický název
# objektu z dostupných OSM tagů") - `name` je nejběžnější/nejspolehlivější,
# ostatní jsou reálné, ale méně časté doplňkové OSM tagy.
_OSM_NAME_TAGS = ("name", "name:cs", "official_name", "alt_name")

_COORDINATE_PATTERN = re.compile(r"^-?\d{1,3}\.\d+\s*,\s*-?\d{1,3}\.\d+$")


def _looks_like_coordinates(text: str) -> bool:
    """Obranná kontrola (zadání bod 11 - "coordinate-only name
    rejection") - `name` tag v praxi souřadnice neobsahuje, ale
    `display_name` se tomuhle vzoru NIKDY nesmí rovnat, ať už by se
    dostal odkudkoliv."""
    return bool(_COORDINATE_PATTERN.match(text.strip()))


def resolve_osm_name(tags: dict[str, str]) -> str | None:
    """Tier 1 - reálný OSM tag, v pořadí priority (zadání bod 1)."""
    for key in _OSM_NAME_TAGS:
        value = tags.get(key)
        if value and value.strip() and not _looks_like_coordinates(value):
            return value.strip()
    return None


@dataclass
class NearestPlace:
    name: str
    distance_km: float


def _nearest(lat: float, lon: float, places: list) -> NearestPlace | None:
    if not places:
        return None
    best = min(places, key=lambda p: haversine_m(lat, lon, p.lat, p.lon))
    return NearestPlace(name=best.name, distance_km=haversine_m(lat, lon, best.lat, best.lon) / 1000.0)


@dataclass
class ResolvedPlaces:
    locality: NearestPlace | None
    municipality: NearestPlace | None


# Velikost prostorového kbelíku pro `PlaceIndex` (zadání "NECHCI nový
# network request" neřeší CPU čas, ale reálně naměřeno na tomhle
# milestone: celostátní import s naivním O(kandidáti × místa) hledáním
# nejbližšího sídla trval ~28 000 s (~7,9 h) nad ~37 500 kandidáty a
# ~41 000 místy. `PlaceIndex` prohledá jen sousední kbelíky, ne
# VŠECHNA místa v ČR pro každého kandidáta. 0.3° je zvolené tak, aby
# 3x3 sousedství kbelíků VŽDY pokrylo `MUNICIPALITY_SEARCH_KM` i pro
# kandidáta na kraji vlastního kbelíku (0.3° >= 20 km i v nejužším
# směru - zeměpisná délka na širší ČR, ~71 km/° při 50° s. š.).
PLACE_BUCKET_SIZE_DEG = 0.3


class PlaceIndex:
    """Prostorový index `PlaceRecord` podle zaokrouhlené lat/lon - viz
    komentář u `PLACE_BUCKET_SIZE_DEG` výše. `resolve_nearby_places()`
    ho použije místo lineárního skenu, pokud je mu předaný (zpětně
    kompatibilní - obyčejný seznam pořád funguje, jen pomaleji)."""

    def __init__(self, places: list):
        self._buckets: dict[tuple[int, int], list] = {}
        for p in places:
            key = (round(p.lat / PLACE_BUCKET_SIZE_DEG), round(p.lon / PLACE_BUCKET_SIZE_DEG))
            self._buckets.setdefault(key, []).append(p)

    def nearby(self, lat: float, lon: float) -> list:
        bkt_lat = round(lat / PLACE_BUCKET_SIZE_DEG)
        bkt_lon = round(lon / PLACE_BUCKET_SIZE_DEG)
        result = []
        for dlat in (-1, 0, 1):
            for dlon in (-1, 0, 1):
                result.extend(self._buckets.get((bkt_lat + dlat, bkt_lon + dlon), []))
        return result


def resolve_nearby_places(lat: float, lon: float, places) -> ResolvedPlaces:
    """`places` - seznam `PlaceRecord` (viz app/national_catalog.py) NEBO
    `PlaceIndex` (rychlejší nad velkým celostátním seznamem) - může
    směšovat oba tiery, rozdělí se tady podle `.tier`."""
    candidates = places.nearby(lat, lon) if isinstance(places, PlaceIndex) else places
    locality_candidates = [p for p in candidates if p.tier == "locality"]
    municipality_candidates = [p for p in candidates if p.tier == "municipality"]
    return ResolvedPlaces(
        locality=_nearest(lat, lon, locality_candidates),
        municipality=_nearest(lat, lon, municipality_candidates),
    )


def resolve_cadastral_area(lat: float, lon: float) -> str | None:
    """NEIMPLEMENTOVÁNO v tomhle milestone (viz docstring modulu) -
    vždy `None`. Funkce existuje jako explicitní krok v pipeline (ne
    tiše vynechaná), aby bylo jasné, KDE by se cadastral lookup zapojil,
    kdyby se ČÚZK RUIAN integrace v budoucnu dodělala."""
    return None


def resolve_display_name(
    osm_tags: dict[str, str],
    candidate_type: str,
    lat: float,
    lon: float,
    places: list,
    resolved_places: ResolvedPlaces | None = None,
) -> tuple[str, str]:
    """Vrátí `(display_name, name_source)` - NIKDY surové souřadnice
    jako název (zadání bod 1). `places` viz `resolve_nearby_places()`.
    `resolved_places` je VOLITELNÝ - pokud ho volající (typicky
    `app.national_catalog.build_candidate_rows()`) už spočítal (potřebuje
    ho i pro `municipality`/`locality` sloupce), předá se rovnou a
    NEPOČÍTÁ se podruhé (výkon - viz `PlaceIndex` docstring)."""
    osm_name = resolve_osm_name(osm_tags)
    if osm_name:
        return osm_name, "osm"

    resolved = resolved_places if resolved_places is not None else resolve_nearby_places(lat, lon, places)
    label = _TYPE_LABEL_CS.get(candidate_type, "Místo")

    # Spojovník místo předložky ("u"/"nad" by vyžadovaly gramatický pád
    # jména sídla - genitiv/instrumentál - který u libovolného
    # českého vlastního jména NELZE deterministicky/bez AI správně
    # skloňovat bez slovníkové/morfologické závislosti navíc, což
    # zadání zakazuje ("nevymýšlej", "nepřidávej zbytečnou
    # infrastrukturu"). Spojovníkový tvar je gramaticky vždy správný
    # (jméno zůstává v 1. pádě, jak je uvedeno v OSM), za cenu menší
    # stylistické přirozenosti - honest trade-off, ne bug.
    if resolved.locality is not None and resolved.locality.distance_km <= NEARBY_LOCALITY_KM:
        return f"{label} – {resolved.locality.name}", "local"

    if resolved.municipality is not None and resolved.municipality.distance_km <= MUNICIPALITY_SEARCH_KM:
        return f"{label} – {resolved.municipality.name}", "municipality"

    cadastral = resolve_cadastral_area(lat, lon)
    if cadastral:
        return f"{label} v k.ú. {cadastral}", "cadastral"

    # Poslední instance (zadání bod 1 - "geograficky odvozený generický
    # název") - v praxi extrémně vzácné v hustě osídlené ČR (MUNICIPALITY_SEARCH_KM
    # pokrývá skoro cokoliv), ale i tak NIKDY souřadnice.
    return f"{label} v Česku", "generated"


def slugify_name_component(text: str) -> str:
    """Pomocník pro budoucí použití (např. group-by municipality) -
    stejná logika jako app/slugs.py::slugify(), zámerně nezávislá kopie
    (place_names.py by jinak muselo importovat slugs.py jen kvůli
    jedné čisté textové funkci)."""
    normalized = unicodedata.normalize("NFKD", text)
    ascii_only = normalized.encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", "-", ascii_only.lower()).strip("-")
