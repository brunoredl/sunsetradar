"""Milestone "Data Foundation" - persistentní SQLite úložiště pro
CELOSTÁTNÍ candidate katalog (zadání bod 3.1). Nahrazuje
`data/candidate_catalog.json` jako zdroj pravdy - stejný důvod, který
`app/candidate_catalog.py` sám předjímal ve svém docstringu: "Kdyby
katalog jednou narostl na desítky tisíc záznamů (celá ČR), SQLite s
prostorovým indexem by dávalo smysl."

ÚMYSLNĚ ŽÁDNÁ SpatiaLite/geo extenze (zadání bod 3.1 - "nepřidávej
zbytečnou infrastrukturu", projekt je lokální): bbox/radius search jsou
obyčejné indexované `lat`/`lon` rozsahové dotazy + (u radius) přesný
haversine filtr v Pythonu nad už zúženou množinou - stejný "levný
prefilter + přesný dopočet" vzor, jaký `app/main.py::api_find_sunset`
už používá nad in-memory seznamem."""

from __future__ import annotations

import math
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from app.candidate_discovery import haversine_m

DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "data" / "national_catalog.sqlite3"

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS candidates (
    id TEXT PRIMARY KEY,
    slug TEXT UNIQUE NOT NULL,
    name TEXT,
    display_name TEXT NOT NULL,
    name_source TEXT NOT NULL,
    candidate_type TEXT NOT NULL,
    lat REAL NOT NULL,
    lon REAL NOT NULL,
    elevation_m REAL,
    region TEXT,
    municipality TEXT,
    locality TEXT,
    cadastral_area TEXT,
    source TEXT NOT NULL DEFAULT 'openstreetmap',
    source_id TEXT,
    near_water INTEGER,
    near_forest INTEGER,
    near_city INTEGER,
    nearest_water_km REAL,
    nearest_forest_km REAL,
    nearest_city_km REAL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_candidates_lat ON candidates(lat);
CREATE INDEX IF NOT EXISTS idx_candidates_lon ON candidates(lon);
CREATE INDEX IF NOT EXISTS idx_candidates_type ON candidates(candidate_type);
CREATE INDEX IF NOT EXISTS idx_candidates_region ON candidates(region);
"""

# Sloupce, které `list_all()`/`*_search()` vrací jako dict - stejné pořadí
# jako zadání bod 7 (plus environment signály z dřívějšího milestone).
_COLUMNS = (
    "id", "slug", "name", "display_name", "name_source", "candidate_type", "lat", "lon", "elevation_m",
    "region", "municipality", "locality", "cadastral_area", "source", "source_id",
    "near_water", "near_forest", "near_city", "nearest_water_km", "nearest_forest_km", "nearest_city_km",
    "created_at", "updated_at",
)


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


def _row_to_dict(row: sqlite3.Row) -> dict:
    d = {col: row[col] for col in _COLUMNS}
    # candidate_type -> `type` v API/frontend odpovědích (zadání bod 7
    # jmenuje pole "type", ale SQL sloupec je "candidate_type" - stejné
    # jako zbytek katalogu odjakživa používal "candidate_type").
    d["candidate_type"] = row["candidate_type"]
    for bool_col in ("near_water", "near_forest", "near_city"):
        if d[bool_col] is not None:
            d[bool_col] = bool(d[bool_col])
    return d


def upsert_candidate(row: dict, db_path: Path = DEFAULT_DB_PATH) -> None:
    upsert_many([row], db_path=db_path)


def upsert_many(rows: list[dict], db_path: Path = DEFAULT_DB_PATH) -> None:
    """Import/update (zadání bod 3.1 - "import, update, deduplikaci")
    - `INSERT ... ON CONFLICT(id) DO UPDATE` = idempotentní upsert,
    stejný `id` (OSM node/way id) se nikdy neduplikuje (zadání bod 6).
    Jedna transakce pro celý batch - výrazně rychlejší než commit po
    řádku při importu tisíců kandidátů."""
    if not rows:
        return
    now = datetime.now(timezone.utc).isoformat()
    conn = _connect(db_path)
    try:
        for row in rows:
            conn.execute(
                """
                INSERT INTO candidates (
                    id, slug, name, display_name, name_source, candidate_type, lat, lon, elevation_m,
                    region, municipality, locality, cadastral_area, source, source_id,
                    near_water, near_forest, near_city, nearest_water_km, nearest_forest_km, nearest_city_km,
                    created_at, updated_at
                ) VALUES (
                    :id, :slug, :name, :display_name, :name_source, :candidate_type, :lat, :lon, :elevation_m,
                    :region, :municipality, :locality, :cadastral_area, :source, :source_id,
                    :near_water, :near_forest, :near_city, :nearest_water_km, :nearest_forest_km, :nearest_city_km,
                    :created_at, :updated_at
                )
                ON CONFLICT(id) DO UPDATE SET
                    slug=excluded.slug, name=excluded.name, display_name=excluded.display_name,
                    name_source=excluded.name_source, candidate_type=excluded.candidate_type,
                    lat=excluded.lat, lon=excluded.lon, elevation_m=excluded.elevation_m, region=excluded.region,
                    municipality=excluded.municipality, locality=excluded.locality, cadastral_area=excluded.cadastral_area,
                    source=excluded.source, source_id=excluded.source_id,
                    near_water=excluded.near_water, near_forest=excluded.near_forest, near_city=excluded.near_city,
                    nearest_water_km=excluded.nearest_water_km, nearest_forest_km=excluded.nearest_forest_km,
                    nearest_city_km=excluded.nearest_city_km, updated_at=excluded.updated_at
                """,
                {
                    "id": row["id"], "slug": row["slug"], "name": row.get("name"),
                    "display_name": row["display_name"], "name_source": row["name_source"],
                    "candidate_type": row["candidate_type"], "lat": row["lat"], "lon": row["lon"],
                    "elevation_m": row.get("elevation_m"), "region": row.get("region"),
                    "municipality": row.get("municipality"), "locality": row.get("locality"),
                    "cadastral_area": row.get("cadastral_area"), "source": row.get("source", "openstreetmap"),
                    "source_id": row.get("source_id"),
                    "near_water": row.get("near_water"), "near_forest": row.get("near_forest"),
                    "near_city": row.get("near_city"), "nearest_water_km": row.get("nearest_water_km"),
                    "nearest_forest_km": row.get("nearest_forest_km"), "nearest_city_km": row.get("nearest_city_km"),
                    "created_at": row.get("created_at", now), "updated_at": now,
                },
            )
        conn.commit()
    finally:
        conn.close()


def get_by_id(candidate_id: str, db_path: Path = DEFAULT_DB_PATH) -> dict | None:
    conn = _connect(db_path)
    try:
        row = conn.execute("SELECT * FROM candidates WHERE id = ?", (candidate_id,)).fetchone()
    finally:
        conn.close()
    return _row_to_dict(row) if row else None


def get_by_slug(slug: str, db_path: Path = DEFAULT_DB_PATH) -> dict | None:
    conn = _connect(db_path)
    try:
        row = conn.execute("SELECT * FROM candidates WHERE slug = ?", (slug,)).fetchone()
    finally:
        conn.close()
    return _row_to_dict(row) if row else None


def list_all(db_path: Path = DEFAULT_DB_PATH) -> list[dict]:
    conn = _connect(db_path)
    try:
        rows = conn.execute("SELECT * FROM candidates").fetchall()
    finally:
        conn.close()
    return [_row_to_dict(r) for r in rows]


def count(db_path: Path = DEFAULT_DB_PATH) -> int:
    conn = _connect(db_path)
    try:
        return conn.execute("SELECT COUNT(*) FROM candidates").fetchone()[0]
    finally:
        conn.close()


def bbox_search(
    south: float, west: float, north: float, east: float,
    candidate_type: str | None = None, db_path: Path = DEFAULT_DB_PATH,
) -> list[dict]:
    """Zadání bod 3.1 - "geografický bounding-box search", indexovaný
    (`idx_candidates_lat`/`idx_candidates_lon`)."""
    query = "SELECT * FROM candidates WHERE lat BETWEEN ? AND ? AND lon BETWEEN ? AND ?"
    params: list = [south, north, west, east]
    if candidate_type:
        query += " AND candidate_type = ?"
        params.append(candidate_type)
    conn = _connect(db_path)
    try:
        rows = conn.execute(query, params).fetchall()
    finally:
        conn.close()
    return [_row_to_dict(r) for r in rows]


def radius_search(
    lat: float, lon: float, radius_km: float,
    candidate_type: str | None = None, db_path: Path = DEFAULT_DB_PATH,
) -> list[dict]:
    """Zadání bod 3.1 - "radius search". Levný bbox prefilter (stupně
    jsou dost blízko lineární na malé vzdálenosti v ČR - nadhodnocený
    obdélník kolem kruhu, nikdy nevynechá platný výsledek) + přesný
    haversine filtr/řazení v Pythonu, stejný vzor jako
    `app/main.py::api_find_sunset`."""
    lat_pad = radius_km / 111.0  # ~111 km/° zeměpisné šířky
    lon_pad = radius_km / (111.0 * max(0.1, math.cos(math.radians(lat))))
    prefiltered = bbox_search(lat - lat_pad, lon - lon_pad, lat + lat_pad, lon + lon_pad, candidate_type, db_path)
    results = []
    for c in prefiltered:
        dist_km = haversine_m(lat, lon, c["lat"], c["lon"]) / 1000.0
        if dist_km <= radius_km:
            results.append({**c, "distance_km": round(dist_km, 2)})
    results.sort(key=lambda c: c["distance_km"])
    return results


def filter_by_type(candidate_type: str, db_path: Path = DEFAULT_DB_PATH) -> list[dict]:
    conn = _connect(db_path)
    try:
        rows = conn.execute("SELECT * FROM candidates WHERE candidate_type = ?", (candidate_type,)).fetchall()
    finally:
        conn.close()
    return [_row_to_dict(r) for r in rows]


def filter_by_region(region: str, db_path: Path = DEFAULT_DB_PATH) -> list[dict]:
    conn = _connect(db_path)
    try:
        rows = conn.execute("SELECT * FROM candidates WHERE region = ?", (region,)).fetchall()
    finally:
        conn.close()
    return [_row_to_dict(r) for r in rows]


def delete_by_id(candidate_id: str, db_path: Path = DEFAULT_DB_PATH) -> None:
    conn = _connect(db_path)
    try:
        conn.execute("DELETE FROM candidates WHERE id = ?", (candidate_id,))
        conn.commit()
    finally:
        conn.close()


def count_by_type(db_path: Path = DEFAULT_DB_PATH) -> dict[str, int]:
    conn = _connect(db_path)
    try:
        rows = conn.execute("SELECT candidate_type, COUNT(*) FROM candidates GROUP BY candidate_type").fetchall()
    finally:
        conn.close()
    return {r[0]: r[1] for r in rows}


def count_by_name_source(db_path: Path = DEFAULT_DB_PATH) -> dict[str, int]:
    conn = _connect(db_path)
    try:
        rows = conn.execute("SELECT name_source, COUNT(*) FROM candidates GROUP BY name_source").fetchall()
    finally:
        conn.close()
    return {r[0]: r[1] for r in rows}
