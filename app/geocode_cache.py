"""Oprava nahlášeného produkčního problému (bezplatné testovací nasazení
na Render Free) - `/api/geocode` dřív volal Nominatim PŘÍMO při KAŽDÉM
požadavku, bez cache a bez jakéhokoliv omezení odchozí frekvence vůči
Nominatimu samotnému. Pod veřejným provozem (víc testerů, opakované
hledání stejných míst) to vedlo k `429 Too Many Requests` od Nominatim,
které appka navíc nerozlišovala od jiných chyb a vracela matoucí `502`.

Řešení, stejný architektonický vzor jako `app/weather_cache.py`
(persistentní SQLite cache s TTL, oddělená od ostatních cache):
1. persistentní cache dotaz -> výsledek (i "nenalezeno"), takže opakované
   hledání STEJNÉHO místa (různými testery i stejným testerem) negeneruje
   žádný další Nominatim požadavek;
2. globální (per proces) throttle NEJMÉŇ 1 s mezi skutečnými odchozími
   Nominatim požadavky - platí i při souběžných požadavcích od víc
   testerů najednou, ne jen "jeden handler = jeden klik";
3. `429`/jiné chyby od Nominatim se rozlišují a appka se NIKDY
   nepokouší automaticky opakovat (žádná retry smyčka) - jasná zpráva
   uživateli, ŽÁDNÝ vymyšlený/tichý výsledek."""

from __future__ import annotations

import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "data" / "geocode_cache.sqlite3"

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
# Nominatim usage policy (operations.osmfoundation.org/policies/nominatim) -
# identifikující User-Agent s kontaktem/odkazem na projekt (ne anonymní
# "dev prototype" string) - appky na sdílených cloudových IP rozsazích
# (Render apod.) Nominatim bez jasné identifikace snáz omezí/zablokuje.
USER_AGENT = "SunsetRadar/0.2 (+https://github.com/brunoredl/sunsetradar; free sunset-viewpoint finder)"

# Nalezené místo se prakticky nemění - dlouhé TTL (zadání "persistentní
# cache... opakované hledání nesmí generovat další externí požadavky").
# "Nenalezeno" má kratší TTL (OSM data se časem doplňují, neopakovat
# navěky stejný negativní výsledek pro překlep, co se mezitím mohl
# opravit v OSM).
FOUND_TTL = timedelta(days=30)
NOT_FOUND_TTL = timedelta(days=1)

# Nominatim fair-use: max 1 request/s. Vynucuje se GLOBÁLNĚ (per proces,
# napříč VŠEMI souběžnými požadavky) přes zámek + minimální rozestup -
# ne jen "doufej, že provoz nebude souběžný".
MIN_REQUEST_INTERVAL_S = 1.1

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS geocode_cache (
    query_key TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    lat REAL,
    lon REAL,
    display_name TEXT,
    cached_at TEXT NOT NULL
);
"""

_throttle_lock = threading.Lock()
_last_request_monotonic = 0.0


class GeocodeNotFoundError(Exception):
    """Nominatim odpověděl, ale pro dotaz nenašel žádné místo."""


class GeocodeRateLimitedError(Exception):
    """Nominatim vrátil 429 - appka se NESMÍ sama pokoušet opakovat,
    jen to poctivě nahlásí volajícímu (viz app/main.py)."""


class GeocodeUnavailableError(Exception):
    """Jiná síťová/HTTP chyba (timeout, 5xx, spojení selhalo apod.)."""


def _normalize_query(q: str) -> str:
    return q.strip().lower()


def _connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db(db_path: Path = DEFAULT_DB_PATH) -> None:
    conn = _connect(db_path)
    try:
        conn.executescript(_SCHEMA_SQL)
        conn.commit()
    finally:
        conn.close()


def _get_cached(query_key: str, db_path: Path) -> sqlite3.Row | None:
    conn = _connect(db_path)
    try:
        row = conn.execute("SELECT * FROM geocode_cache WHERE query_key = ?", (query_key,)).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    cached_at = datetime.fromisoformat(row["cached_at"])
    ttl = FOUND_TTL if row["status"] == "found" else NOT_FOUND_TTL
    if datetime.now(timezone.utc) - cached_at > ttl:
        return None  # stale - volající to zpracuje jako cache miss
    return row


def _save_cached(query_key: str, status: str, lat: float | None, lon: float | None, display_name: str | None, db_path: Path) -> None:
    conn = _connect(db_path)
    try:
        conn.execute(
            """
            INSERT INTO geocode_cache (query_key, status, lat, lon, display_name, cached_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(query_key) DO UPDATE SET
                status=excluded.status, lat=excluded.lat, lon=excluded.lon,
                display_name=excluded.display_name, cached_at=excluded.cached_at
            """,
            (query_key, status, lat, lon, display_name, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
    finally:
        conn.close()


def _throttled_nominatim_get(query: str) -> requests.Response:
    """Zajistí NEJMÉŇ `MIN_REQUEST_INTERVAL_S` mezi skutečnými odchozími
    požadavky na Nominatim, bez ohledu na to, kolik vláken/požadavků se
    o to souběžně pokouší (zámek drží i přes samotné `requests.get()`,
    aby se dva souběžné cache-miss požadavky nemohly "proklouznout"
    oba hned za sebou pod minimálním rozestupem)."""
    global _last_request_monotonic
    with _throttle_lock:
        wait_s = MIN_REQUEST_INTERVAL_S - (time.monotonic() - _last_request_monotonic)
        if wait_s > 0:
            time.sleep(wait_s)
        response = requests.get(
            NOMINATIM_URL,
            params={"q": query, "format": "json", "countrycodes": "cz", "limit": 1},
            headers={"User-Agent": USER_AGENT},
            timeout=10,
        )
        _last_request_monotonic = time.monotonic()
        return response


def geocode_cached(query: str, db_path: Path = DEFAULT_DB_PATH) -> dict:
    """Cache-aware geokódování - JEDINÉ místo, které by `app/main.py`
    mělo volat (stejný vzor jako `weather_cache.fetch_weather_cached()`).
    Vrátí `{"lat", "lon", "display_name"}`, nebo vyhodí jednu ze tří
    výjimek výš - volající (webová vrstva) je převede na HTTP odpověď."""
    query_key = _normalize_query(query)
    cached = _get_cached(query_key, db_path)
    if cached is not None:
        if cached["status"] == "not_found":
            raise GeocodeNotFoundError(query)
        return {"lat": cached["lat"], "lon": cached["lon"], "display_name": cached["display_name"]}

    try:
        response = _throttled_nominatim_get(query)
    except requests.exceptions.RequestException as exc:
        # Síťová chyba/timeout - NEcachuje se (přechodné, příští pokus
        # může uspět) a appka se o to sama znovu nepokouší.
        raise GeocodeUnavailableError(str(exc)) from exc

    if response.status_code == 429:
        raise GeocodeRateLimitedError("Nominatim vrátil 429 (rate limit)")
    try:
        response.raise_for_status()
    except requests.exceptions.HTTPError as exc:
        raise GeocodeUnavailableError(str(exc)) from exc

    results = response.json()
    if not results:
        _save_cached(query_key, "not_found", None, None, None, db_path)
        raise GeocodeNotFoundError(query)

    r = results[0]
    lat, lon, display_name = float(r["lat"]), float(r["lon"]), r["display_name"]
    _save_cached(query_key, "found", lat, lon, display_name, db_path)
    return {"lat": lat, "lon": lon, "display_name": display_name}
