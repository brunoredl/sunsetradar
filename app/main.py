"""Fáze 5 - první použitelné UI nad `analyze_location()`.

DŮLEŽITÉ (architektura, viz zadání bod 8/16): tenhle soubor je ČISTĚ
prezentační vrstva. Jediný vstupní bod do výpočetního jádra je
`analyze_location()` - žádný route handler tady nevolá DEM provider,
horizon algoritmus, solar algoritmus ani visibility algoritmus přímo.
Serializace `LocationAnalysis` do JSON (`_serialize_value`/`_to_dict`)
NENÍ výpočet - jen převod už hotových dat do transportního formátu."""

from __future__ import annotations

import json
import logging
import math
import threading
import time
from dataclasses import fields, is_dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo, available_timezones

import requests
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, field_validator

from app import catalog_db
from app import national_grid
from app import regions as region_defs
from app import terrain_cache
from app import weather_cache
from app.analyze_location import (
    DEFAULT_ANALYSIS_RANGE_M,
    DEFAULT_PIXEL_SIZE_M,
    Diagnostics,
    LocationAnalysis,
    analyze_location,
    compute_dynamic_sunset,
)
from app.best_days import DEFAULT_BEST_DAYS_COUNT, compute_best_days
from app.candidate_catalog import load_candidate_catalog
from app.environment_context import classify_candidate_environment, discover_environment_features_cached
from app.personalization import UserPreferences, compute_user_match, estimate_access_level
from app.slugs import find_by_slug
from app.solar import make_observer
from app.sunset_rating import compute_sunset_rating
from app.sunset_score import (
    SunsetScoreResult,
    classify_color_potential,
    classify_general_horizon_view,
    classify_general_inversion_potential,
    classify_general_sky,
    classify_inversion_potential,
    compute_full_sunset_score,
)
from app.terrain_worker import get_worker
from app.weather_provider import WeatherUnavailableError
from app.cuzk_dem import OutsideCoverageError
from app.dem import OutOfBoundsError

logger = logging.getLogger(__name__)

class _NoCacheStaticFiles(StaticFiles):
    """BUG NALEZENÝ (živé použití, Milník 12) - výchozí `StaticFiles`
    nenastavuje `Cache-Control`, takže prohlížeč cachuje `/static/*.js`
    podle vlastní heuristiky (typicky agresivně) - po úpravě `app.js`
    pak uživatel reálně dostal STAROU verzi skriptu (odkazující na dávno
    smazaný `#today-sky-badge`) i přes čerstvě vykreslené nové HTML,
    což se projevilo jako `TypeError: null is not an object` zabalený
    do "Nepodařilo se spojit se serverem" chybové hlášky (zavádějící -
    vypadalo to jako síťová chyba, byl to stale JS). `no-cache` (NE
    `no-store`) donutí prohlížeč vždy znovu ověřit přes ETag/
    Last-Modified (Starlette to podporuje nativně - 304 když se soubor
    nezměnil, tedy pořád rychlé), ale nikdy neukáže tiše starý obsah po
    reálné změně souboru - důležité hlavně pro dev s `--reload`."""

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response


# Příprava bezplatného testovacího nasazení - ABSOLUTNÍ cesta (ne
# "static"/"templates" relativně k CWD) - startovací příkaz na hostingu
# nemusí vždy běžet přesně z kořene repozitáře, zatímco `__file__` je
# vždy spolehlivě tady (zadání bod 3 - "ověř, že se appka spustí bez
# přístupu k souborům z mého počítače", tedy robustně vůči prostředí).
_APP_ROOT = Path(__file__).resolve().parent.parent

app = FastAPI(title="Sunset Radar")
app.mount("/static", _NoCacheStaticFiles(directory=str(_APP_ROOT / "static")), name="static")
templates = Jinja2Templates(directory=str(_APP_ROOT / "templates"))


def static_url(filename: str) -> str:
    """BULLETPROOF doplněk k `_NoCacheStaticFiles` výše - `Cache-Control:
    no-cache` řeší věci do budoucna, ale NEOPRAVÍ prohlížeč, který si
    URL `/static/app.js` už stihl označit za "fresh" podle vlastní
    heuristiky PŘED touhle změnou (přesně tenhle případ nastal živě -
    uživatel furt viděl starý `app.js` i po přidání `no-cache` hlavičky,
    protože prohlížeč o revalidaci ani nepožádal). Připojením `?v=<mtime
    souboru>` na konec URL se mění SAMOTNÁ URL při každé změně souboru -
    nová URL = nový cache klíč, žádná stará cache ji nemůže "znát jako
    fresh", ať si prohlížeč myslí cokoliv. `os.path.getmtime()` je
    levné (jeden stat() syscall), volá se jen při renderu stránky, ne
    při každém requestu na statický soubor samotný."""
    path = _APP_ROOT / "static" / filename
    try:
        version = int(path.stat().st_mtime)
    except OSError:
        version = 0
    return f"/static/{filename}?v={version}"


templates.env.globals["static_url"] = static_url

# Milestone "Persistent Terrain Cache" - zajistí schéma při startu a
# uklidí "running" záznamy z případného spadlého předchozího běhu
# procesu (zadání bod 17 - "cache musí přežít restart", ne zůstat
# navěky trčet jako 'zpracovává se' bez žádného workeru, který by to
# skutečně zpracovával).
terrain_cache.init_db()
# Milestone "Complete Product Build" - weather cache je ÚMYSLNĚ
# samostatná SQLite databáze (data/weather_cache.sqlite3), ne stejná
# tabulka jako terrain (zadání bod 7 - "odděl static/dynamic").
weather_cache.init_db()


class PreferencesModel(BaseModel):
    """Zadání bod 13/14/24 - uživatelské preference, VOLITELNÉ (jejich
    absence != "žádný match", viz app/personalization.py). Posílá je
    frontend při hledání/detailu, pokud je uživatel nastavil."""

    scene_types: list[str] = []
    photography: bool = False
    dramatic: bool = False
    clear: bool = False
    hiking_effort: str | None = None
    # Milník 12 (zadání bod 4) - viz app/personalization.py UserPreferences.
    inversion: bool = False
    vivid_colors: bool = False
    sun_on_horizon: bool = False
    panorama: bool = False

    def to_domain(self) -> UserPreferences:
        return UserPreferences(
            scene_types=self.scene_types, photography=self.photography,
            dramatic=self.dramatic, clear=self.clear, hiking_effort=self.hiking_effort,
            inversion=self.inversion, vivid_colors=self.vivid_colors,
            sun_on_horizon=self.sun_on_horizon, panorama=self.panorama,
        )


def _display_name(c: dict) -> str:
    """Milestone "Data Foundation" - `display_name` (name-resolution
    pipeline, app/place_names.py) je odteď VŽDY to, co se zobrazuje
    uživateli - `name` zůstává surový OSM tag (může být prázdný/None).
    Fallback na `name` je jen pro starší JSON katalog (zpětná
    kompatibilita, viz app/candidate_catalog.py)."""
    return c.get("display_name") or c.get("name") or "Neznámé místo"


def _dem_quality_fields(dem) -> dict:
    """Milestone "Trustworthy Results with Partial Cache" - DEM kvalita
    (viz Milestone "DEM Quality and Resolution Control",
    app/terrain_cache.py `dem_resolution_m` sloupec) se dřív nikam z
    cache nepropisovala do API odpovědi - uživatel/frontend ji neměl
    šanci zohlednit. `reduced_dem_quality` používá STEJNÝ práh
    (`DEFAULT_PIXEL_SIZE_M * 1.001`) jako diagnostické varování v
    `app/analyze_location.py::compute_static_terrain()` a odmítnutí
    horšího přepisu v `app/terrain_cache.py::save_ready()` - žádná nová
    vymyšlená hranice, jen znovupoužití už existující."""
    return {
        "dem_resolution_m": round(dem.resolution_m, 1),
        "dem_fallback_used": dem.fallback_used,
        "reduced_dem_quality": dem.resolution_m > DEFAULT_PIXEL_SIZE_M * 1.001,
    }


def _composite_label(score: int) -> str:
    """Zadání bod 9 - popisek CELKOVÉHO Sunset Score (terrain + weather +
    ...), ÚMYSLNĚ jinak formulovaný než `SunsetRating.label` (to mluví
    jen o terénu - "Excellent terrain visibility" by bylo zavádějící
    pro skóre, které zahrnuje i počasí)."""
    if score >= 85:
        return "Excellent conditions"
    if score >= 70:
        return "Good conditions"
    if score >= 50:
        return "Fair conditions"
    if score >= 30:
        return "Poor conditions"
    return "Unfavorable conditions"


def _compute_full_score(
    analysis: LocationAnalysis, elevation_m: float, tz_name: str
) -> SunsetScoreResult:
    """Sdílené mezi `/api/analyze` a `/api/find_sunset` (zadání bod 43 -
    "nepřidávej endpoint jen aby jich bylo víc", sdílej logiku). Weather
    fetch selhání NIKDY nespadne celý request - `compute_full_sunset_score()`
    reweightuje váhy mezi dostupné komponenty (zadání bod 32)."""
    observer = make_observer(analysis.location.latitude, analysis.location.longitude, elevation_m)
    try:
        weather = weather_cache.fetch_weather_cached(
            analysis.location.latitude, analysis.location.longitude, analysis.solar.astronomical_sunset, timezone_name=tz_name,
        )
    except WeatherUnavailableError as exc:
        logger.warning("Počasí nedostupné pro (%s, %s): %s", analysis.location.latitude, analysis.location.longitude, exc)
        weather = None
    return compute_full_sunset_score(analysis, weather, observer, analysis.solar.astronomical_sunset, elevation_m)

# Fáze 5, bod 4 - testovací lokality. Souřadnice převzaté z už dřív
# programově nalezených a validovaných bodů (viz PLAN.md Krok 12/22-23,
# scripts/find_test_points.py) - žádné nové odhadované souřadnice.
TEST_LOCATIONS = [
    {"name": "Milešovka (otevřený vrchol)", "lat": 50.5552, "lon": 13.9312},
    {"name": "Krušné hory (horský hřeben)", "lat": 50.3972, "lon": 12.9479},
    {"name": "Labská niva (horské údolí)", "lat": 50.5764, "lon": 13.7167},
    {"name": "Milešovka - úbočí (otevřenější terén)", "lat": 50.6556, "lon": 13.9417},
    {"name": "Paříž (zahraniční lokalita)", "lat": 48.8566, "lon": 2.3522},
]

DEFAULT_TIMEZONE = "Europe/Prague"


class AnalyzeRequest(BaseModel):
    lat: float
    lon: float
    date: str  # YYYY-MM-DD
    timezone: str = DEFAULT_TIMEZONE
    preferences: PreferencesModel | None = None
    distance_km: float | None = None  # jen pro user_match hiking/access odhad (zadání bod 14) - volitelné, ne z terrain enginu

    @field_validator("lat")
    @classmethod
    def _validate_lat(cls, v: float) -> float:
        if not (-90.0 <= v <= 90.0):
            raise ValueError("latitude musí být v rozsahu -90 až 90")
        return v

    @field_validator("lon")
    @classmethod
    def _validate_lon(cls, v: float) -> float:
        if not (-180.0 <= v <= 180.0):
            raise ValueError("longitude musí být v rozsahu -180 až 180")
        return v

    @field_validator("date")
    @classmethod
    def _validate_date(cls, v: str) -> str:
        try:
            date.fromisoformat(v)
        except ValueError as exc:
            raise ValueError(f"neplatné datum '{v}' - očekávám formát YYYY-MM-DD") from exc
        return v

    @field_validator("timezone")
    @classmethod
    def _validate_timezone(cls, v: str) -> str:
        # Bezpečnostní poznámka (zadání bod 19): `ZoneInfo` sám o sobě čte
        # jen ze systémové/tzdata databáze podle jména, ne z libovolné
        # cesty na disku - `available_timezones()` navíc dopředu odmítne
        # cokoliv, co není platný IANA název, takže sem nemůže proklouznout
        # path traversal ani nic mimo whitelist skutečných zón.
        if v not in available_timezones():
            raise ValueError(f"neznámá časová zóna '{v}'")
        return v


# Příprava bezplatného testovacího nasazení (Render Free) - zadání bod 5:
# "veřejný provoz nemůže spouštět neomezeně drahé výpočty... opakované
# požadavky nevyvolají nekontrolované zatížení externích zdrojů". Jediné
# dva SKUTEČNĚ drahé endpointy jsou `/api/analyze` (živý DEM download +
# terén, 11-32 s/studený bod, viz README) a `/api/find_sunset` (enqueue
# do terrain workeru pro až `MAX_TERRAIN_ANALYSIS_CANDIDATES` bodů) -
# `/api/geocode` navíc přímo volá externí Nominatim (má vlastní fair-use
# politiku). Jednoduchý in-memory sliding-window limiter per IP - ŽÁDNÁ
# nová závislost (Redis/slowapi), pro jeden proces na free tieru stačí;
# limit se resetuje při restartu procesu, což je na tomhle měřítku v
# pořádku (zadání "co nejjednodušší", ne enterprise rate limiting).
class _RateLimiter:
    def __init__(self, max_requests: int, window_s: float):
        self.max_requests = max_requests
        self.window_s = window_s
        self._hits: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def check(self, key: str) -> bool:
        """True = povoleno, False = limit překročen (volající vrátí 429)."""
        now = time.monotonic()
        with self._lock:
            hits = [t for t in self._hits.get(key, []) if now - t < self.window_s]
            if len(hits) >= self.max_requests:
                self._hits[key] = hits
                return False
            hits.append(now)
            self._hits[key] = hits
            return True


def _client_key(request: Request) -> str:
    # Render (a většina hostingů) terminuje TLS přes proxy - skutečná
    # IP klienta je v `X-Forwarded-For` (první hodnota), `request.client.host`
    # by jinak pořád ukazoval na interní IP proxy (jeden klíč pro VŠECHNY
    # uživatele = rate limit by efektivně nefungoval).
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


# Limity jsou záměrně velkorysé pro NORMÁLNÍ použití (pár testerů),
# ale brání triviálnímu skriptovanému zahlcení - viz komentáře výše.
_analyze_limiter = _RateLimiter(max_requests=20, window_s=60.0)
_find_sunset_limiter = _RateLimiter(max_requests=20, window_s=60.0)
_geocode_limiter = _RateLimiter(max_requests=15, window_s=60.0)


@app.get("/healthz")
def healthz():
    """Zadání bod 3 - jednoduchý health-check, ÚMYSLNĚ bez dotazu na
    katalog/terén/DEM (ty mají vlastní poctivé chybové stavy, viz jinde)
    - jen potvrzuje, že proces žije a umí odpovědět."""
    return {"status": "ok"}


@app.get("/")
def index(request: Request):
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "test_locations": TEST_LOCATIONS,
            "default_timezone": DEFAULT_TIMEZONE,
            "default_analysis_range_m": DEFAULT_ANALYSIS_RANGE_M,
            "today": date.today().isoformat(),
        },
    )


@app.post("/api/analyze")
def api_analyze(req: AnalyzeRequest, request: Request):
    if not _analyze_limiter.check(_client_key(request)):
        return JSONResponse(
            {"error": "Příliš mnoho požadavků na analýzu z tvé IP - zkus to prosím za chvíli znovu."},
            status_code=429,
        )
    """Jediné místo, kde webová vrstva volá výpočetní jádro - přesně
    `analyze_location()`, nic jiného (viz docstring modulu)."""
    d = date.fromisoformat(req.date)  # už validováno v AnalyzeRequest
    tz = ZoneInfo(req.timezone)  # už validováno v AnalyzeRequest
    when = datetime(d.year, d.month, d.day, 12, 0, 0, tzinfo=tz)

    try:
        result = analyze_location(req.lat, req.lon, when)
    except OutOfBoundsError as exc:
        return JSONResponse({"error": f"Zadaný bod je mimo pokrytí dostupných DEM dat: {exc}"}, status_code=422)
    except OutsideCoverageError as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    except RuntimeError as exc:
        # Poslední DEM zdroj selhal a žádný další fallback neexistuje
        # (viz app.dem_strategy / app.analyze_location) - jasná chyba
        # uživateli, NIKDY tichý/falešný výsledek.
        return JSONResponse({"error": f"Nepodařilo se získat terénní data pro tenhle bod: {exc}"}, status_code=502)
    except requests.exceptions.RequestException as exc:
        # BUG NALEZENÝ A OPRAVENÝ (Fáze 5): bod bez pokrytí posledního DEM
        # zdroje (typicky oceán - Copernicus tam nemá dlaždici vůbec, S3
        # vrátí 404) dřív skončil neošetřenou requests.exceptions.HTTPError
        # a syrovým HTTP 500 bez jakékoliv zprávy - viz PLAN.md Fáze 5.
        # `analyze_location()`/`resolve_dem()` tuhle chybu záměrně
        # nepotlačují (žádný další fallback neexistuje) - je na webové
        # vrstvě ji převést na srozumitelnou odpověď.
        #
        # AUDIT NÁLEZ (validační fáze): dřív se do `{exc}` interpolovala
        # syrová `requests` výjimka (obsahuje interní S3 URL DEM zdroje) -
        # to se posílalo přímo uživateli. Technický detail teď jde jen do
        # server logu, uživatel dostane čistě lidskou zprávu bez URL/detailů.
        logger.warning("DEM download failed for lat=%s lon=%s: %s", req.lat, req.lon, exc)
        return JSONResponse(
            {"error": "Terrain data unavailable for this location (no DEM coverage, e.g. open ocean, or the data source is unreachable)."},
            status_code=502,
        )
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)

    return _serialize_analysis(result, tz_name=req.timezone, preferences=req.preferences, distance_km=req.distance_km)


# Fáze 8 (rozšířeno Fáze 11 - regionální architektura, zadání bod B) -
# "Find the best sunset in Czechia". ÚMYSLNĚ jen ČTE předpočítané
# datasety ze souboru - NEPOČÍTÁ terrain analýzu naživo pro víc
# kandidátů najednou (zadání bod 14 - naměřeno ~18 s/kandidát za
# studena, viz PLAN.md a scripts/build_candidate_dataset.py; u velmi
# otevřených vrcholů s dlouhým dohledem naměřeno i řádově MINUTY na
# kandidáta, viz Fáze 11 audit u Krkonoš - živý HTTP request by u
# jakéhokoliv víc-kandidátního hledání dávno překročil rozumný timeout).
# Každý region = bbox (pro budoucí prostorové filtrování/výběr regionu,
# zadání bod G) + cesta k předpočítanému JSON datasetu. Přidání regionu
# = jeden řádek sem + spuštění scripts/build_candidate_dataset.py,
# ŽÁDNÝ nový kód. Zatím JEN Beskydy + Krkonoše (zadání bod U - "neplň
# všechny regiony automaticky").
# Milestone "Real Candidate Catalog" - bbox/name teď žijí JEN v
# app/regions.py (dřív duplikováno i tady i ve
# scripts/build_candidate_dataset.py - audit to označil jako věc k
# sjednocení). `dataset_path` zůstává specifické pro webovou vrstvu
# (kde na disku leží PŘEDPOČÍTANÝ per-region dataset), takže žije tady.
DATA_DIR = Path(__file__).resolve().parent.parent / "data"

REGIONS = {
    key: {"name": r.name, "bbox": r.bbox, "dataset_path": DATA_DIR / f"candidates_{key}.json"}
    for key, r in region_defs.REGIONS.items()
}
CANDIDATE_DATASETS = {key: r["dataset_path"] for key, r in REGIONS.items()}

MAX_SEARCH_RADIUS_KM = 100.0
# Fáze 11 bod I - "co když radius=50km narazí na stovky kandidátů":
# tenhle strop je čistě obranný a při současné velikosti dat (desítky
# kandidátů na dva registrované regiony) se nikdy nemůže uplatnit -
# žádné škálování/precompute infrastruktura navíc není potřeba, dokud
# reálný počet kandidátů v radiusu tenhle strop skutečně nezačne
# přibližovat (zadání bod T - "neměř naslepo").
MAX_SEARCH_RESULTS = 200


@app.get("/api/candidates")
def api_candidates(region: str = "beskydy"):
    path = CANDIDATE_DATASETS.get(region)
    if path is None:
        return JSONResponse(
            {"error": f"Neznámá oblast '{region}'. Dostupné: {', '.join(CANDIDATE_DATASETS)}."},
            status_code=404,
        )
    if not path.exists():
        return JSONResponse(
            {"error": f"Pro oblast '{region}' zatím není předpočítaný dataset - spusť scripts/build_candidate_dataset.py."},
            status_code=404,
        )
    return json.loads(path.read_text())


@app.get("/api/geocode")
def api_geocode(q: str, request: Request):
    """Textová lokalita -> souřadnice (zadání bod C - "WHERE SHOULD WE
    SEARCH"). Nominatim (OpenStreetMap) - zdarma, bez API klíče, ODbL
    licence, stejný zdroj jako candidate discovery/mapové dlaždice.

    AUDIT (Fáze 11 bod C, před implementací): Nominatim usage policy
    (https://operations.osmfoundation.org/policies/nominatim/) žádá
    max. 1 request/s a identifikující User-Agent - vyhovuje, protože
    tenhle endpoint volá backend JEN při explicitním odeslání "Where"
    pole (jeden klik uživatele = jeden request), NIKDY jako
    autocomplete-při-psaní. Volá se ze SERVERU (ne přímo z prohlížeče),
    aby šel nastavit správný User-Agent a aby to nebylo blokované CORS
    politikou Nominatim.

    Nasazovací audit (bezplatné testovací nasazení) - navíc per-IP
    limiter (`_geocode_limiter`), aby veřejný provoz nemohl Nominatim
    zahltit nad rámec jeho fair-use politiky (viz výše)."""
    if not _geocode_limiter.check(_client_key(request)):
        return JSONResponse({"error": "Příliš mnoho požadavků na vyhledávání - zkus to prosím za chvíli znovu."}, status_code=429)
    q = q.strip()
    if not q:
        return JSONResponse({"error": "Zadej název místa."}, status_code=400)
    try:
        response = requests.get(
            "https://nominatim.openstreetmap.org/search",
            params={"q": q, "format": "json", "countrycodes": "cz", "limit": 1},
            headers={"User-Agent": "SunsetRadar/0.1 (dev prototype)"},
            timeout=10,
        )
        response.raise_for_status()
        results = response.json()
    except requests.exceptions.RequestException as exc:
        logger.warning("Geocoding selhal pro dotaz '%s': %s", q, exc)
        return JSONResponse({"error": "Vyhledávání místa teď není dostupné, zkus to prosím znovu."}, status_code=502)
    if not results:
        return JSONResponse({"error": f"Místo '{q}' se nepodařilo najít."}, status_code=404)
    r = results[0]
    return {"lat": float(r["lat"]), "lon": float(r["lon"]), "display_name": r["display_name"]}


# Milestone "Nationwide Live Search" - kolik z nalezených-ale-nehodnocených
# kandidátů smí /api/search za JEDEN request nejvýš zařadit do background
# workeru (stejný ochranný důvod jako MAX_TERRAIN_ANALYSIS_CANDIDATES u
# /api/find_sunset - nezahltit frontu/DEM zdroj jedním requestem s velkým
# radiusem nad hustě pokrytou oblastí).
MAX_SEARCH_ENQUEUE = 15


def _rated_search_entry(c: dict, cache_key: str, when: datetime, worker) -> tuple[dict, bool]:
    """Pro jednoho kandidáta z `catalog_db.radius_search()` vrátí
    (entry, needs_enqueue). `entry` má VŽDY stejný tvar (zadání bod 3 -
    "poctivě odděl nezpracované") - `rating`/`sunset_time`/... jsou
    `None` a `rating_status="unrated"`, pokud terén ještě není v cache;
    NIKDY fake/odhadnutá hodnota. `needs_enqueue=True` znamená "cache
    miss, volající smí zvážit worker.enqueue() podle vlastního stropu"
    - tahle funkce sama o sobě nikdy nezařazuje (to dělá volající, s
    MAX_SEARCH_ENQUEUE stropem, aby i při velkém výsledku šlo nanejvýš
    pár kandidátů do fronty za request)."""
    base = {
        "id": c["id"], "slug": c.get("slug"), "name": _display_name(c), "type": c["candidate_type"],
        "lat": c["lat"], "lon": c["lon"], "elevation_m": c.get("elevation_m"), "region": c.get("region"),
        "near_water": c.get("near_water"), "near_forest": c.get("near_forest"), "near_city": c.get("near_city"),
        "distance_km": c["distance_km"],
        "rating": None, "rating_label": None, "rating_status": "unrated",
        "sunset_time": None, "sunset_azimuth_deg": None, "delay_minutes": None, "terrain_obstruction": None,
    }
    base.update({"dem_resolution_m": None, "dem_fallback_used": None, "reduced_dem_quality": None})
    status = terrain_cache.get_status(c["id"], cache_key)
    if status != terrain_cache.STATUS_READY:
        return base, status != terrain_cache.STATUS_RUNNING  # running = už se čeká, missing/failed = hodno nové fronty
    static = terrain_cache.get_static_terrain(c["id"], cache_key)
    if static is None:  # závod se smazáním/přepsáním mezi get_status a get_static_terrain - ošetři jako miss
        return base, True
    # Milestone "Trustworthy Results with Partial Cache" - dřív se
    # tohle NEOŠETŘOVALO (žádný try/except) - jediný kandidát s chybou
    # v dynamickém dopočtu by shodil CELÝ `/api/search` request na 500,
    # i když statický terén (drahá část) byl v pořádku. Teď se taková
    # chyba poctivě vykáže jako `rating_status: "error"` (NE "unrated" -
    # to by znamenalo "ještě nezpracováno", tohle je "zpracováno a
    # nevyšlo") a zbytek výsledků se vrátí normálně.
    try:
        solar, visibility, dyn_warnings = compute_dynamic_sunset(static, when)
        analysis = LocationAnalysis(
            location=static.location, dem=static.dem, terrain=static.terrain, solar=solar, visibility=visibility,
            diagnostics=Diagnostics(warnings=static.warnings + dyn_warnings, calculation_parameters={}),
        )
        # ÚMYSLNĚ jen terrain-only `compute_sunset_rating()` (BEZE ZMĚNY), ne
        # plný `sunset_score` kompozit - tohle pole se jmenuje `rating` od
        # vzniku tohoto endpointu (viz historický tvar `data/candidates_*.json`)
        # a `/api/find_sunset`/place page už mají svoje vlastní `sunset_score`
        # pro plné skóre; zachování významu pole je zadání bod "zachovej
        # kontrakty", ne přehlédnutí.
        rating = compute_sunset_rating(analysis)
    except Exception:
        logger.exception("Dynamická sunset analýza selhala pro %s i přes cache hit (/api/search)", c["id"])
        base["rating_status"] = "error"
        base.update(_dem_quality_fields(static.dem))
        return base, False
    base.update({
        "rating": rating.overall_0_100, "rating_label": rating.label, "rating_status": "ready",
        "sunset_time": solar.astronomical_sunset.isoformat(), "sunset_azimuth_deg": solar.sunset_azimuth_deg,
        "delay_minutes": visibility.delay_minutes, "terrain_obstruction": visibility.terrain_obstruction,
    })
    base.update(_dem_quality_fields(static.dem))
    return base, False


@app.get("/api/search")
def api_search(lat: float, lon: float, radius_km: float = 25.0, candidate_type: str | None = None):
    """Milestone "Nationwide Live Search" (nahrazuje Fáze 11 bod F/H) -
    kandidáti v okolí zadaného středu z CELOSTÁTNÍHO katalogu
    (`catalog_db.radius_search()` - indexovaný SQL bbox prefilter +
    přesný haversine dopočet, ŽÁDNÉ načtení všech 37k záznamů do
    Pythonu, zadání bod 2/4), ne jen ze 3 ručně vybraných regionů.

    Rating je teď ČESTNĚ dvoustavový (zadání bod 3): kandidáti se
    SKUTEČNĚ hotovým terénem (`terrain_cache` status `ready`) dostanou
    reálný `compute_sunset_rating()` a řadí se první (`-rating`,
    `distance_km`); kandidáti BEZ hotového terénu jsou `rating_status:
    "unrated"` s `rating: null`, zařazeni AŽ ZA všechny ohodnocené,
    řazeni jen podle vzdálenosti - nikdy ne vydáváni za "nejlepší
    místo". Cache-miss kandidáti (max `MAX_SEARCH_ENQUEUE` za request)
    se zařadí do STEJNÉHO background workeru jako `/api/find_sunset` -
    druhé zavolání stejného místa je pak už reálně ohodnocené."""
    if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
        return JSONResponse({"error": "Neplatné souřadnice středu hledání."}, status_code=400)
    radius_km = max(1.0, min(radius_km, MAX_SEARCH_RADIUS_KM))

    if catalog_db.count() == 0:
        return JSONResponse(
            {"error": "Centrální katalog kandidátů zatím neexistuje - spusť scripts/build_national_catalog.py."},
            status_code=404,
        )

    in_radius = catalog_db.radius_search(lat, lon, radius_km, candidate_type=candidate_type)
    if not in_radius:
        return {
            "center": {"lat": lat, "lon": lon}, "radius_km": radius_km,
            "candidate_count": 0, "truncated": False, "candidates": [],
        }

    truncated = len(in_radius) > MAX_SEARCH_RESULTS
    # `radius_search()` už řadí podle vzdálenosti vzestupně (zadání bod
    # 4 - "omez výběr v SQL") - nejbližší MAX_SEARCH_RESULTS je tedy
    # přesně ten samý výběr, který by vyšel i z plného seznamu.
    in_radius = in_radius[:MAX_SEARCH_RESULTS]

    cache_key = terrain_cache.compute_cache_key()
    when = datetime.now(ZoneInfo(DEFAULT_TIMEZONE)).replace(hour=12, minute=0, second=0, microsecond=0)
    worker = get_worker()

    rated: list[dict] = []
    unrated: list[dict] = []
    enqueued = 0
    for c in in_radius:
        entry, needs_enqueue = _rated_search_entry(c, cache_key, when, worker)
        (rated if entry["rating_status"] == "ready" else unrated).append(entry)
        if needs_enqueue and enqueued < MAX_SEARCH_ENQUEUE:
            if worker.enqueue(c["id"], c["lat"], c["lon"], cache_key, c.get("region"), c.get("candidate_type")):
                enqueued += 1

    rated.sort(key=lambda e: (-e["rating"], e["distance_km"]))
    unrated.sort(key=lambda e: e["distance_km"])

    return {
        "center": {"lat": lat, "lon": lon},
        "radius_km": radius_km,
        "candidate_count": len(rated) + len(unrated),
        "rated_count": len(rated),
        "unrated_count": len(unrated),
        "enqueued_count": enqueued,
        "truncated": truncated,
        "candidates": rated + unrated,
    }


# ============================= Milestone "Persistent Terrain Cache +
# Background Precomputation" - /api/find_sunset (PŘEPSÁNO) =============
# Předchozí verze (milestone "Real Candidate Catalog") dělala terrain
# analýzu NAŽIVO za requestu (i s wall-clock time budgetem) - reálný
# benchmark ukázal, že u nepokrytých oblastí to stihlo jen 2-8 z 15
# kandidátů za 60s. Tenhle milestone to řeší persistentní SQLite cache
# (app/terrain_cache.py): request teď NIKDY nečeká na DEM/horizon výpočet -
# u cache hitu jen dopočítá levnou dynamickou (datum-závislou) část,
# u cache missu kandidáta zařadí do background workeru
# (app/terrain_worker.py) a vrátí, co je SKUTEČNĚ hotové HNED.
MAX_TERRAIN_ANALYSIS_CANDIDATES = 15


class FindSunsetRequest(BaseModel):
    lat: float
    lon: float
    radius_km: float = 25.0
    timezone: str = DEFAULT_TIMEZONE
    candidate_type: str | None = None
    preferences: PreferencesModel | None = None
    # Milestone "Immediate Full Results" - zadání bod "pokud se seznam
    # musí stránkovat... musí existovat možnost zobrazit další". Strop
    # `MAX_SEARCH_RESULTS` na `other_candidates` zůstává (payload musí
    # mít rozumnou velikost), ale kandidáty NAD stropem analýzy (`status:
    # "not_analyzed"`) jde dostránkovat - `to_check` (max 15, vždy celé)
    # offset nepotřebuje, offset se týká JEN "not_analyzed" zbytku.
    other_candidates_offset: int = 0

    @field_validator("lat")
    @classmethod
    def _validate_lat(cls, v: float) -> float:
        if not (-90.0 <= v <= 90.0):
            raise ValueError("latitude musí být v rozsahu -90 až 90")
        return v

    @field_validator("lon")
    @classmethod
    def _validate_lon(cls, v: float) -> float:
        if not (-180.0 <= v <= 180.0):
            raise ValueError("longitude musí být v rozsahu -180 až 180")
        return v

    @field_validator("other_candidates_offset")
    @classmethod
    def _validate_offset(cls, v: int) -> int:
        return max(0, v)


def _dynamic_result_from_cache(
    c: dict, static: "terrain_cache.StaticTerrain", when: datetime, distance_km: float, tz_name: str, preferences: PreferencesModel | None
) -> dict:
    """Dopočítá LEVNOU (datum-závislou) část z už uloženého statického
    terénu a poskládá stejný `LocationAnalysis` tvar, jaký by vrátila
    `analyze_location()` naživo (zadání bod 7 - "výsledek musí být
    ekvivalentní cold analysis") - jen bez opakovaného DEM/horizon
    výpočtu. Milestone "Complete Product Build" navíc dopočítá plný
    Sunset Score (weather+terrain+...) a user match, stejnou sdílenou
    logikou jako `/api/analyze` (`_compute_full_score()`)."""
    solar, visibility, dyn_warnings = compute_dynamic_sunset(static, when)
    analysis = LocationAnalysis(
        location=static.location,
        dem=static.dem,
        terrain=static.terrain,
        solar=solar,
        visibility=visibility,
        diagnostics=Diagnostics(warnings=static.warnings + dyn_warnings, calculation_parameters={}),
    )
    rating = compute_sunset_rating(analysis)  # beze změny - terrain-only, technický detail (Why-list/panorama)
    full_score = _compute_full_score(analysis, static.location.elevation_m, tz_name)
    # Milník 12 (zadání bod 2/3) - stejná reálná `full_score.weather` data
    # jako `sunset_type` výše, jen novým lensem (barva/inverze místo
    # "typu"). Specifické PRO TENTO bod (elevace konkrétního kandidáta),
    # na rozdíl od `general_*` protějšků v `_serialize_analysis()`.
    color_potential = classify_color_potential(full_score.weather)
    inversion_potential = classify_inversion_potential(full_score.weather, static.location.elevation_m)

    user_match_payload = None
    if preferences is not None:
        prefs = preferences.to_domain()
        match = compute_user_match(
            prefs, candidate_type=c["candidate_type"], elevation_m=static.location.elevation_m, distance_km=distance_km,
            sunset_type_key=full_score.sunset_type.key,
            photography_score_0_100=next((comp.value_0_100 for comp in full_score.components if comp.key == "photography"), None),
            near_water=c.get("near_water"), near_forest=c.get("near_forest"), near_city=c.get("near_city"),
            color_potential_key=color_potential.key, inversion_key=inversion_potential.key,
            horizon_at_azimuth_0_100=rating.components.horizon_at_azimuth,
            sector_openness_0_100=rating.components.sector_openness,
        )
        user_match_payload = _to_json(match)

    result = {
        "id": c["id"],
        "slug": c.get("slug"),
        "name": _display_name(c),
        "lat": c["lat"],
        "lon": c["lon"],
        "type": c["candidate_type"],
        "distance_km": round(distance_km, 1),
        "measured_elevation_m": static.location.elevation_m,
        "hike_access_estimate": estimate_access_level(distance_km, static.location.elevation_m),
        "near_water": c.get("near_water"),
        "near_forest": c.get("near_forest"),
        "near_city": c.get("near_city"),
        # `rating`/`rating_label` zůstávají terrain-only (zpětná kompatibilita
        # se stávajícím UI - markery/karty dřív četly jen tohle pole).
        "rating": rating.overall_0_100,
        "rating_label": rating.label,
        # `rating_detail` (place page, zadání bod 2 - "Why this place?"
        # dynamicky generováno z reálných dat) - PLNÝ terrain rating objekt
        # včetně `.components` (horizon_at_azimuth/sector_openness/duration/
        # delay), navíc k flat `rating`/`rating_label` výše (žádná změna
        # jejich tvaru, jen doplněk pro place page).
        "rating_detail": _to_json(rating),
        # NOVÉ - plný kompozitní Sunset Score je od týhle chvíle to, podle
        # čeho se řadí výsledky a vybírá Best Sunset (zadání bod 8/9).
        "sunset_score": full_score.overall_0_100,
        "sunset_score_label": _composite_label(full_score.overall_0_100),
        "sunset_type": _to_json(full_score.sunset_type),
        "score_components": _to_json(full_score.components),
        "weather": _to_json(full_score.weather),
        # Milník 12 (zadání bod 2/3) - per-místo barevný potenciál / potenciál
        # inverze-mlhy, ODDĚLENĚ od `sunset_score`/`user_match` (zadání "nesmí
        # tajně přepisovat základní skóre") - čistě informativní pole navíc.
        "color_potential": _to_json(color_potential),
        "inversion_potential": _to_json(inversion_potential),
        "user_match": user_match_payload,
        "sunset_time": solar.astronomical_sunset.isoformat(),
        "sunset_azimuth_deg": solar.sunset_azimuth_deg,
        "delay_minutes": visibility.delay_minutes,
        "terrain_obstruction": visibility.terrain_obstruction,
        "terrain": {
            "azimuths_deg": static.terrain.azimuths_deg,
            "horizon_altitudes_deg": static.terrain.horizon_altitudes_deg,
            "slope_deg": static.terrain.slope_deg,
            "aspect_deg": static.terrain.aspect_deg,
        },
    }
    # Milestone "Trustworthy Results with Partial Cache" - DEM kvalita
    # (viz _dem_quality_fields()) dřív z téhle funkce nikam neunikla,
    # ačkoliv `static.dem.resolution_m` ji od Milestone "DEM Quality and
    # Resolution Control" už správně nese. Používá `/api/find_sunset` i
    # `/api/place/{slug}` (sdílená funkce).
    result.update(_dem_quality_fields(static.dem))
    return result


@app.post("/api/find_sunset")
def api_find_sunset(req: FindSunsetRequest, request: Request):
    """Synchronní, RYCHLÝ endpoint (zadání bod 24 - "search nesmí být
    primárně limitovaný DEM downloadem"). Flow (zadání bod 11):
    geografický filtr přes CELÝ katalog -> levný prefilter (nejbližší
    první, žádná preference typu) -> per-kandidát CACHE LOOKUP ->
    ready: dopočítej dynamickou část a vrať; missing/failed: zařaď do
    background workeru a NEČEKEJ. Best Sunset (zadání bod 13) může být
    jen z `results` (= vždy jen ready kandidáti), nikdy z odhadu.

    Milestone "Nationwide Live Search": geografický filtr teď běží
    přímo v SQL (`catalog_db.radius_search()` - indexovaný bbox
    prefilter + přesný haversine dopočet/řazení) nad CELÝM národním
    katalogem (37k+ kandidátů), ne nad kompletním seznamem načteným do
    Pythonu (`load_candidate_catalog()` dřív načetla/projela VŠECH 37k
    záznamů v Pythonu i pro malý radius - zadání bod 2/4, "neprováděj
    kompletní načtení a seřazení v Pythonu, pokud lze omezit výběr v
    SQL"). Chování pro volajícího beze změny - `found_count` je pořád
    "kolik padlo do radiusu", `to_check` pořád nejbližší
    `MAX_TERRAIN_ANALYSIS_CANDIDATES`.

    Nasazovací audit - per-IP limiter (`_find_sunset_limiter`), stejný
    důvod jako u `/api/analyze` (enqueue do terrain workeru pro až
    `MAX_TERRAIN_ANALYSIS_CANDIDATES` bodů není zadarmo)."""
    if not _find_sunset_limiter.check(_client_key(request)):
        return JSONResponse(
            {"error": "Příliš mnoho požadavků na hledání z tvé IP - zkus to prosím za chvíli znovu."},
            status_code=429,
        )
    if catalog_db.count() == 0:
        return JSONResponse(
            {"error": "Centrální katalog kandidátů zatím neexistuje - spusť scripts/build_national_catalog.py."},
            status_code=404,
        )

    radius_km = max(1.0, min(req.radius_km, MAX_SEARCH_RADIUS_KM))
    in_radius = catalog_db.radius_search(req.lat, req.lon, radius_km, candidate_type=req.candidate_type)

    found_count = len(in_radius)
    if found_count == 0:
        # Milestone "Correct Coverage Semantics and Candidate Accounting" -
        # tenhle early-return dřív neměl `failed_count` vůbec (jiný tvar
        # odpovědi než neprázdný případ níže) - konzistence počítadel
        # musí platit i pro prázdný radius, ne jen pro neprázdný.
        return {
            "found_count": 0, "to_analyze_count": 0, "ready_count": 0, "enqueued_count": 0, "pending_count": 0,
            "failed_count": 0, "previously_failed_count": 0, "fully_covered": True, "results": [],
            "other_candidates": [], "other_candidates_truncated": False,
            "other_candidates_remaining": 0, "other_candidates_next_offset": 0,
        }

    # `radius_search()` už vrací řazené podle vzdálenosti vzestupně
    # (zadání bod 9/J beze změny - ŽÁDNÁ subjektivní preference typu,
    # jen "nejbližší se zpracují dřív").
    to_check = in_radius[:MAX_TERRAIN_ANALYSIS_CANDIDATES]

    cache_key = terrain_cache.compute_cache_key()
    when = datetime.now(ZoneInfo(req.timezone)).replace(hour=12, minute=0, second=0, microsecond=0)
    worker = get_worker()

    # Milestone "Immediate Full Results" - AUDIT NÁLEZ (reprodukováno
    # živě, reálné hledání "Brno", radius 5 km): `found_count` 64,
    # `to_analyze_count` 15 (strop `MAX_TERRAIN_ANALYSIS_CANDIDATES`,
    # záměrný a zachovaný), ale `results` dřív obsahovalo JEN skutečně
    # `ready` kandidáty - typicky 1-2 z těch 15 (`TERRAIN_WORKER_CONCURRENCY=1`
    # zpracovává frontu sekvenčně, desítky sekund/kandidát). Zbylých ~13
    # z `to_check` a VŠECH 49 kandidátů NAD stropem 15 se v odpovědi
    # neobjevilo NIKDE jinak než jako souhrnná čísla. `other_candidates`
    # tohle doplňuje - KAŽDÝ nalezený kandidát se dostane do odpovědi s
    # PRAVDIVÝM stavem, NIKDY s vymyšleným skóre. Čtyři odlišené stavy
    # (zadání bod "Vyhodnocuje se" vs. "Čeká na vyhodnocení" vs.
    # "Analýza se nezdařila" vs. kandidáti nad stropem):
    #   "evaluating"   - worker na tom PRÁVĚ TEĎ pracuje (STATUS_RUNNING)
    #   "waiting"      - zařazeno/čeká ve frontě, worker se ještě nedostal
    #   "failed"       - poslední pokus selhal (automaticky se zkouší znovu)
    #   "not_analyzed" - nad stropem `to_check`, tento request se o něj
    #                    vůbec nepokusil (žádný enqueue, žádná DEM zátěž navíc)
    other_candidates: list[dict] = []

    def _other(c: dict, status: str) -> None:
        if len(other_candidates) >= MAX_SEARCH_RESULTS:
            return
        other_candidates.append({
            "id": c["id"], "slug": c.get("slug"), "name": _display_name(c), "type": c["candidate_type"],
            "lat": c["lat"], "lon": c["lon"], "distance_km": c["distance_km"], "status": status,
        })

    results: list[dict] = []
    enqueued_count = 0
    pending_count = 0
    failed_count = 0
    previously_failed_count = 0  # Milestone "Trustworthy Results with Partial Cache" - viz níže
    for c in to_check:
        status = terrain_cache.get_status(c["id"], cache_key)
        if status == terrain_cache.STATUS_READY:
            static = terrain_cache.get_static_terrain(c["id"], cache_key)
            if static is None:  # závod: mezitím smazáno/přepsáno - ošetři jako miss, neshoď request
                # Milestone "Correct Coverage Semantics" - dřív se sem
                # trefený kandidát NEPOČÍTAL ani do `enqueued_count`,
                # ani do `pending_count`, pokud `worker.enqueue()`
                # vrátilo False (už zařazeno jinde) - zmizel z účtování
                # úplně, takže `ready+enqueued+pending+failed` nemuselo
                # sedět na `to_analyze_count`. Stejný was_new vzor jako
                # v "missing nebo failed" větvi níže.
                was_new = worker.enqueue(c["id"], c["lat"], c["lon"], cache_key, c.get("region"), c.get("candidate_type"))
                if was_new:
                    enqueued_count += 1
                else:
                    pending_count += 1
                _other(c, "waiting")
                continue
            try:
                results.append(_dynamic_result_from_cache(c, static, when, c["distance_km"], req.timezone, req.preferences))
            except Exception:
                logger.exception("Dynamická sunset analýza selhala pro %s i přes cache hit", c["id"])
                failed_count += 1
                _other(c, "failed")
        elif status == terrain_cache.STATUS_RUNNING:
            pending_count += 1
            _other(c, "evaluating")
        else:  # missing nebo failed - obojí je (beze změny chování) hodné nového pokusu
            if status == terrain_cache.STATUS_FAILED:
                # Milestone "Trustworthy Results with Partial Cache" - dřív
                # se tohle tiše slilo do stejného "enqueued/pending" čísla
                # jako úplně nové kandidáty, takže uživatel viděl jen "se
                # připravuje na pozadí" i pro místo, kde DEM data selhala
                # (třeba trvale mimo pokrytí) - zavádějící "jen čekej".
                # Retry chování se NEMĚNÍ (pořád se zkouší znovu, stejná
                # DEM zátěž jako dřív), jen se to navíc počítá samostatně
                # pro poctivější zprávu ("u X míst se poslední pokus
                # nezdařil", ne jen generické "se připravuje").
                previously_failed_count += 1
            was_new = worker.enqueue(c["id"], c["lat"], c["lon"], cache_key, c.get("region"), c.get("candidate_type"))
            if was_new:
                enqueued_count += 1
            else:
                pending_count += 1  # už zařazeno z dřívějšího requestu
            # Milestone "Immediate Full Results" - "failed" status se
            # ukazuje i tady (ne jen "waiting") POKUD poslední pokus
            # selhal - uživatel má vidět "Analýza se nezdařila" S
            # vyznačenou automatickou opravou (retry už proběhl výše),
            # ne jen obecné "čeká".
            _other(c, "failed" if status == terrain_cache.STATUS_FAILED else "waiting")

    # Kandidáti NAD stropem `MAX_TERRAIN_ANALYSIS_CANDIDATES` - tenhle
    # request se o ně vůbec nepokusil (žádný enqueue, žádná DEM zátěž
    # navíc - strop zůstává zachovaný), ale v radiusu reálně jsou, takže
    # musí být vidět jako "zatím nevyhodnoceno", ne zmizet beze stopy.
    # Milestone "Immediate Full Results" - `other_candidates_offset`
    # dostránkuje zbytek, pokud ho nešlo vrátit celý najednou
    # (`MAX_SEARCH_RESULTS` strop na velikost odpovědi, zadání bod "musí
    # existovat možnost zobrazit další").
    beyond_cap = in_radius[len(to_check):]
    offset = min(req.other_candidates_offset, len(beyond_cap))
    other_candidates_truncated = False
    not_analyzed_added = 0
    for c in beyond_cap[offset:]:
        if len(other_candidates) >= MAX_SEARCH_RESULTS:
            other_candidates_truncated = True
            break
        _other(c, "not_analyzed")
        not_analyzed_added += 1
    other_candidates_remaining = max(0, len(beyond_cap) - offset - not_analyzed_added)
    other_candidates_next_offset = offset + not_analyzed_added

    # Zadání bod 15 - "BASE SUNSET SCORE + USER MATCH = BEST SUNSET FOR
    # YOU": fyzikální `sunset_score` NIKDY nezmění svou hodnotu kvůli
    # preferencím, ale VÝBĚR/POŘADÍ výsledků smí preference zohlednit
    # (jinak by byly jen dekorativní checkboxy bez efektu, zadání bod 24).
    def _rank_key(r: dict) -> tuple:
        match_pct = (r.get("user_match") or {}).get("match_pct")
        combined = 0.5 * r["sunset_score"] + 0.5 * match_pct if match_pct is not None else r["sunset_score"]
        return (-combined, r["distance_km"], r["name"])

    results.sort(key=_rank_key)

    # Milestone "Correct Coverage Semantics and Candidate Accounting" -
    # PŘESNÁ definice `fully_covered`: `results` (jen SKUTEČNĚ
    # ohodnocené kandidáty, beze změny) je "nejlepší ze VŠECH v
    # radiusu" pravdivě JEN tehdy, když:
    #   (a) žádný kandidát v radiusu nebyl odříznutý limitem
    #       `MAX_TERRAIN_ANALYSIS_CANDIDATES` (`found_count <= to_analyze_count`,
    #       tedy `to_check` == `in_radius` beze zbytku - `catalog_db.radius_search()`
    #       sama o sobě žádný LIMIT nemá, `found_count` je VŽDY úplný
    #       počet v radiusu, ne jen vzorek - ověřeno čtením
    #       app/catalog_db.py, ne odvozeno z názvu),
    #   (b) žádný z `to_check` ještě nečeká na dokončení
    #       (`pending_count == enqueued_count == 0`), A
    #   (c) žádný z `to_check` NESELHAL v dynamickém dopočtu
    #       (`failed_count == 0`) - tohle dřív CHYBĚLO: kandidát se
    #       `ready` statickým terénem, ale selhaným dynamickým dopočtem
    #       (viz `failed_count` výše) byl vynechaný z `results`, ale
    #       `fully_covered` by si ho nevšimla, protože není ani
    #       `pending_count`, ani `enqueued_count` - mohla být tedy
    #       chybně `True`, i když reálně relevantní kandidát v radiusu
    #       zůstal BEZ platného hodnocení.
    # `previously_failed_count` samo o sobě fully_covered nemusí
    # testovat navíc - každý "previously failed" kandidát prochází
    # STEJNOU enqueue/pending logikou jako nový kandidát (viz smyčka
    # výše), takže už je zahrnutý v (b).
    #
    # Pokud je `fully_covered` False, "nejlepší z `results`" je
    # pravdivě jen "nejlepší ZE SKUTEČNĚ VYHODNOCENÉ PODMNOŽINY", nikdy
    # "nejlepší v celém radiusu" - frontend tenhle rozdíl používá k
    # volbě textu (viz static/app.js a static/explore.js).
    fully_covered = found_count <= len(to_check) and pending_count == 0 and enqueued_count == 0 and failed_count == 0

    return {
        "found_count": found_count,
        "to_analyze_count": len(to_check),
        "ready_count": len(results),
        "enqueued_count": enqueued_count,
        "pending_count": pending_count,
        "failed_count": failed_count,
        "previously_failed_count": previously_failed_count,
        "fully_covered": fully_covered,
        "results": results,
        # Milestone "Immediate Full Results" - KAŽDÝ nalezený kandidát
        # mimo `results` (vyhodnocuje se, čeká, selhal, nebo je nad
        # stropem analýzy) - viz `_other()` výše. `status` je jedno z
        # "evaluating"/"waiting"/"failed"/"not_analyzed", NIKDY
        # skóre/pořadí (žádné z nich nebylo skutečně vyhodnoceno).
        # `other_candidates_remaining`/`_next_offset` - zadání bod "musí
        # existovat možnost zobrazit další", pokud `other_candidates`
        # samo nestačí na celý zbytek nad stropem analýzy (`MAX_SEARCH_RESULTS`
        # strop na velikost odpovědi) - frontend poslé `other_candidates_offset:
        # other_candidates_next_offset` pro další "stránku".
        "other_candidates": other_candidates,
        "other_candidates_truncated": other_candidates_truncated,
        "other_candidates_remaining": other_candidates_remaining,
        "other_candidates_next_offset": other_candidates_next_offset,
    }


# ============================= Milestone "Complete Product Build"
# (pokračování) - place pages `/sunset/{slug}/` (zadání bod 2, MANDATORY)
# =============================
def _candidate_summary(c: dict) -> dict:
    return {
        "id": c["id"], "slug": c["slug"], "name": _display_name(c), "type": c["candidate_type"],
        "lat": c["lat"], "lon": c["lon"], "elevation_m": c.get("elevation_m"), "region": c.get("region"),
        "municipality": c.get("municipality"), "locality": c.get("locality"),
        "near_water": c.get("near_water"), "near_forest": c.get("near_forest"), "near_city": c.get("near_city"),
    }


@app.get("/sunset/{slug}/")
def place_page(request: Request, slug: str):
    """SSR shell (jméno/typ hned v <title>, zbytek dotažen přes
    `/api/place/{slug}` - zadání bod 2). 404 stránka, pokud slug
    neexistuje v katalogu (žádný tichý prázdný render)."""
    catalog = load_candidate_catalog()
    candidate = find_by_slug(catalog, slug)
    if candidate is None:
        return templates.TemplateResponse(request, "place_not_found.html", {"slug": slug}, status_code=404)
    return templates.TemplateResponse(
        request, "place.html", {"candidate": _candidate_summary(candidate), "default_timezone": DEFAULT_TIMEZONE},
    )


@app.get("/api/place/{slug}")
def api_place(slug: str, timezone: str = DEFAULT_TIMEZONE, distance_km: float | None = None):
    """Zadání bod 2/3 - VŠECHNA data pro place page: Hero/Tonight/Why/Score
    breakdown/Terrain/Access + Best Days This Week, ze STEJNÉHO cache
    lookup vzoru jako `/api/find_sunset` (cache hit -> dopočet; cache
    miss -> enqueue + poctivé "pending", NIKDY vymyšlený výsledek).
    `distance_km` je VOLITELNÝ (typicky proklik z Explore/Tonight
    výsledku, kde je skutečná vzdálenost od hledaného středu známá) -
    bez něj `hike_access_estimate` zůstává `None` (zadání bod 2 -
    "Access odhad, nikdy vydávaný za přesná trail data" - bez
    referenčního bodu bychom si vzdálenost museli vymyslet)."""
    catalog = load_candidate_catalog()
    candidate = find_by_slug(catalog, slug)
    if candidate is None:
        return JSONResponse({"error": f"Místo '{slug}' nebylo v katalogu nalezeno."}, status_code=404)

    cache_key = terrain_cache.compute_cache_key()
    status = terrain_cache.get_status(candidate["id"], cache_key)
    static = terrain_cache.get_static_terrain(candidate["id"], cache_key) if status == terrain_cache.STATUS_READY else None

    if static is None:
        worker = get_worker()
        was_new = worker.enqueue(
            candidate["id"], candidate["lat"], candidate["lon"], cache_key, candidate.get("region"), candidate.get("candidate_type"),
        )
        return {
            "status": "pending",
            "candidate": _candidate_summary(candidate),
            "was_newly_enqueued": was_new,
        }

    when = datetime.now(ZoneInfo(timezone)).replace(hour=12, minute=0, second=0, microsecond=0)
    tonight = _dynamic_result_from_cache(candidate, static, when, distance_km or 0.0, timezone, None)
    if distance_km is None:
        # Poctivost (zadání bod 14/32) - bez skutečného referenčního bodu
        # by "hike_access_estimate"/"distance_km" byly vymyšlené číslo.
        tonight["hike_access_estimate"] = None
        tonight["distance_km"] = None

    best_days = compute_best_days(static, timezone, num_days=DEFAULT_BEST_DAYS_COUNT, today=when)

    return {
        "status": "ready",
        "candidate": _candidate_summary(candidate),
        "tonight": tonight,
        "best_days": [
            {"date": d.date_iso, "sunset_time": d.sunset_time.isoformat(), "score": _to_json(d.score)}
            for d in best_days
        ],
    }


@app.get("/explore")
def explore_page(request: Request):
    return templates.TemplateResponse(request, "explore.html", {"default_timezone": DEFAULT_TIMEZONE})


def _environment_signal_for_point(lat: float, lon: float) -> tuple[bool | None, bool | None, bool | None]:
    """Stejný reálný Water/Forest/City signál jako candidate katalog
    (`app/environment_context.py`), ale pro LIBOVOLNÝ bod (`/api/analyze`
    nemá vždy candidate_id - třeba klik na mapu mimo katalog). Milestone
    "Data Foundation" - najde dlaždici celostátní mřížky (ne jen staré 3
    regiony), jejíž bbox bod obsahuje, a znovupoužije JIŽ NA DISKU
    cachovaná Overpass data pro tu dlaždici (typický případ - žádný nový
    síťový požadavek). Pokud dlaždice cache nemá (např. selhaný fetch
    při celostátním importu), `discover_environment_features_cached()` by
    zkusila živý dotaz - to by kliku na mapu nemělo shodit odpověď, proto
    `try/except` s poctivým `(None, None, None)` fallbackem."""
    for cell in national_grid.generate_grid_cells():
        south, west, north, east = cell.bbox
        if south <= lat <= north and west <= lon <= east:
            try:
                features = discover_environment_features_cached(cell.key, cell.bbox)
            except Exception:
                return None, None, None
            env = classify_candidate_environment(lat, lon, features)
            return env.near_water, env.near_forest, env.near_city
    return None, None, None


def _serialize_analysis(
    result: LocationAnalysis, tz_name: str = "UTC", preferences: PreferencesModel | None = None, distance_km: float | None = None
) -> dict:
    payload = _to_json(result)
    # Milestone "Trustworthy Results with Partial Cache" - stejné
    # odvozené pole jako u /api/find_sunset/_api/search (_dem_quality_fields()),
    # aby frontend nemusel duplikovat práh pro "degradované rozlišení"
    # na dalším místě (result.dem.resolution_m/fallback_used tu samy o
    # sobě zůstávají beze změny, jen `reduced_dem_quality` navíc).
    payload["dem"]["reduced_dem_quality"] = result.dem.resolution_m > DEFAULT_PIXEL_SIZE_M * 1.001
    # Fáze 6.6 - Sunset Rating (viz app/sunset_rating.py), BEZE ZMĚNY -
    # `rating` dál znamená přesně to, co dřív (čistě terénní geometrie),
    # zpětně kompatibilní se stávajícím UI (rating ring, Why-list,
    # Technical details). Milestone "Complete Product Build" (zadání
    # bod 8/9) přidává NOVÉ pole `sunset_score` = plný kompozit
    # (terrain+weather+visibility+atmosphere+sun_position+photography) -
    # tohle je nová "headline" hodnota, `rating` zůstává technický detail.
    rating = compute_sunset_rating(result)
    payload["rating"] = _to_json(rating)

    full_score = _compute_full_score(result, result.location.elevation_m, tz_name)
    score_payload = _to_json(full_score)
    score_payload["overall_0_100"] = full_score.overall_0_100
    score_payload["label"] = _composite_label(full_score.overall_0_100)
    payload["sunset_score"] = score_payload
    # Milník 12 (zadání bod 2/3) - per-místo (tenhle konkrétní `result`,
    # ne "obecně" - viz `general_color_potential`/`general_inversion_potential`
    # níže pro tu obecnou verzi) barevný potenciál a potenciál inverze/mlhy.
    color_potential = classify_color_potential(full_score.weather)
    inversion_potential = classify_inversion_potential(full_score.weather, result.location.elevation_m)
    payload["color_potential"] = _to_json(color_potential)
    payload["inversion_potential"] = _to_json(inversion_potential)

    # Milník "Postupné vylepšení" (Fáze 2) - "obecná předpověď západu
    # slunce jako jevu, nezávislá na konkrétním vyhlídkovém místě".
    # Znovupoužívá STEJNOU `classify_sunset_type()`/stejná reálná
    # `WeatherInfo` data jako `sunset_score.sunset_type` výše (žádná
    # nová heuristika, žádný nový síťový dotaz) - jen volaná bez
    # terénu/fotografie, viz app/sunset_score.py. `general_sky`/
    # `general_horizon_view` jsou ÚMYSLNĚ odlišené od `sunset_score.sunset_type`
    # (ten zůstává terén-specifický pro KONKRÉTNÍ bod/vyhlídku).
    payload["general_sky"] = _to_json(classify_general_sky(full_score.weather))
    payload["general_horizon_view"] = _to_json(classify_general_horizon_view(full_score.weather))
    # Milník 12 (zadání bod 2) - barva nemá terénní vstup vůbec (viz
    # classify_color_potential), takže "obecná" a "pro tenhle bod"
    # verze jsou vždy OBSAHOVĚ stejné - pole se přesto jmenuje konzistentně
    # s `general_sky`/`general_horizon_view`/`general_inversion_potential`,
    # aby frontend hero sekce četla jednotnou sadu `general_*` polí.
    payload["general_color_potential"] = payload["color_potential"]
    payload["general_inversion_potential"] = _to_json(classify_general_inversion_potential(full_score.weather))

    if distance_km is not None:
        payload["hike_access_estimate"] = estimate_access_level(distance_km, result.location.elevation_m)

    if preferences is not None:
        prefs = preferences.to_domain()
        near_water, near_forest, near_city = _environment_signal_for_point(result.location.latitude, result.location.longitude)
        match = compute_user_match(
            prefs,
            candidate_type="peak",  # /api/analyze nemá vždy candidate_type (může to být libovolný klik na mapu) - neutrální výchozí
            elevation_m=result.location.elevation_m,
            distance_km=distance_km or 0.0,
            sunset_type_key=full_score.sunset_type.key,
            photography_score_0_100=next((c.value_0_100 for c in full_score.components if c.key == "photography"), None),
            near_water=near_water, near_forest=near_forest, near_city=near_city,
            color_potential_key=color_potential.key, inversion_key=inversion_potential.key,
            horizon_at_azimuth_0_100=rating.components.horizon_at_azimuth,
            sector_openness_0_100=rating.components.sector_openness,
        )
        payload["user_match"] = _to_json(match)

    return payload


def _to_json(value):
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, timedelta):
        return {"seconds": value.total_seconds(), "minutes": round(value.total_seconds() / 60, 1)}
    if isinstance(value, float) and math.isnan(value):
        return None  # čisté JSON nemá NaN - NULL je poctivější než tichy vymyšlené číslo
    if isinstance(value, (list, tuple)):
        return [_to_json(v) for v in value]
    if isinstance(value, dict):
        return {k: _to_json(v) for k, v in value.items()}
    if is_dataclass(value) and not isinstance(value, type):
        out = {}
        for f in fields(value):
            if f.name.startswith("_"):
                continue  # např. TerrainInfo._profile - interní pomůcka, ne pro přenos
            out[f.name] = _to_json(getattr(value, f.name))
        return out
    return value
