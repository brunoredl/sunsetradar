"""Milestone "Persistent Terrain Cache + Background Precomputation".

AUDIT (před implementací, viz PLAN.md): `analyze_location()` (teď
rozdělená na `compute_static_terrain()` + `compute_dynamic_sunset()`,
viz app/analyze_location.py) má jasnou hranici - `compute_static_terrain()`
závisí JEN na (lat, lon, analysis_range_m, azimuth_step_deg,
horizon_step_m, observer_eye_height_m, pixel_size_m,
refraction_coefficient), NIKDY na datu/čase. Tohle je přesně to, co
tenhle modul persistentně cachuje - jeden vrchol má stejný horizont
dnes i za rok, dokud se nezmění DEM zdroj nebo algoritmus samotný.

PROČ SQLite (zadání bod 5): potřebujeme rychlý lookup podle
(candidate_id, cache_key), per-záznamový UPDATE (status
running/ready/failed) a status tracking pro background worker -
přesně to, k čemu je SQLite vhodné, a co by s jedním velkým JSON
souborem vyžadovalo přepis celého souboru při každé změně jednoho
záznamu. Při ~2000 (časem desítky tisíc) kandidátech je SQLite
naprosto dostatečné - žádná externí databáze, žádný server, jeden
soubor na disku (`data/terrain_cache.sqlite3`), stdlib `sqlite3`."""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from app.analyze_location import (
    DEFAULT_AZIMUTH_STEP_DEG,
    DEFAULT_HORIZON_STEP_M,
    DEFAULT_OBSERVER_EYE_HEIGHT_M,
    DEFAULT_PIXEL_SIZE_M,
    DemInfo,
    LocationInfo,
    StaticTerrain,
    TerrainInfo,
    compute_static_terrain,
)
from app.dem_strategy import DEFAULT_ANALYSIS_RANGE_M
from app.horizon import DEFAULT_REFRACTION_COEFFICIENT, HorizonProfile

logger = logging.getLogger(__name__)

DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "data" / "terrain_cache.sqlite3"

# Zadání bod 4/16 - "když se změní terrain engine nebo parametr, stará
# cache nesmí být omylem považována za validní". Zvyšuje se RUČNĚ při
# jakékoliv změně, která by mohla ovlivnit výsledek `compute_static_terrain()`
# (horizon algoritmus, Earth curvature/refrakce konstanty, DEM
# strategie/fallback pořadí...) - ne automaticky odvozováno z hashe
# zdrojového kódu (jednodušší, explicitní, žádné falešné invalidace
# kvůli nesouvisející změně komentáře).
TERRAIN_ENGINE_VERSION = "1"
TERRAIN_CACHE_SCHEMA_VERSION = 1

STATUS_MISSING = "missing"  # není v cache vůbec (implicitní stav, žádný řádek)
STATUS_RUNNING = "running"
STATUS_READY = "ready"
STATUS_FAILED = "failed"


def compute_cache_key(
    analysis_range_m: float = DEFAULT_ANALYSIS_RANGE_M,
    azimuth_step_deg: float = DEFAULT_AZIMUTH_STEP_DEG,
    horizon_step_m: float = DEFAULT_HORIZON_STEP_M,
    observer_eye_height_m: float = DEFAULT_OBSERVER_EYE_HEIGHT_M,
    pixel_size_m: float = DEFAULT_PIXEL_SIZE_M,
    refraction_coefficient: float = DEFAULT_REFRACTION_COEFFICIENT,
) -> str:
    """Deterministický klíč - zahrnuje VŠECHNY parametry, které
    `compute_static_terrain()` skutečně bere (odvozeno z reálného kódu
    signatury, ne z dohadu, zadání bod 4), plus verze enginu/schématu.
    DEM provider/dataset NENÍ součástí klíče - není to VSTUPNÍ parametr
    (`resolve_dem()` ho odvozuje deterministicky ze souřadnic), je to
    VÝSLEDEK, který se ukládá do záznamu pro transparentnost/debug, ne
    do klíče (viz PLAN.md pro plné zdůvodnění)."""
    payload = {
        "engine_version": TERRAIN_ENGINE_VERSION,
        "schema_version": TERRAIN_CACHE_SCHEMA_VERSION,
        "analysis_range_m": analysis_range_m,
        "azimuth_step_deg": azimuth_step_deg,
        "horizon_step_m": horizon_step_m,
        "observer_eye_height_m": observer_eye_height_m,
        "pixel_size_m": pixel_size_m,
        "refraction_coefficient": refraction_coefficient,
    }
    raw = json.dumps(payload, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS terrain_cache (
    candidate_id TEXT NOT NULL,
    cache_key TEXT NOT NULL,
    status TEXT NOT NULL,
    lat REAL NOT NULL,
    lon REAL NOT NULL,
    region TEXT,
    candidate_type TEXT,
    elevation_m REAL,
    dem_provider TEXT,
    dem_dataset TEXT,
    dem_resolution_m REAL,
    analysis_range_m REAL,
    dem_fallback_used INTEGER,
    dem_fallback_reason TEXT,
    slope_deg REAL,
    aspect_deg REAL,
    azimuths_json TEXT,
    horizon_altitudes_json TEXT,
    min_horizon_deg REAL,
    max_horizon_deg REAL,
    static_warnings_json TEXT,
    error_message TEXT,
    compute_duration_s REAL,
    computed_at TEXT,
    PRIMARY KEY (candidate_id, cache_key)
);
CREATE INDEX IF NOT EXISTS idx_terrain_cache_status ON terrain_cache(status);
CREATE INDEX IF NOT EXISTS idx_terrain_cache_region ON terrain_cache(region);
"""


def _connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")  # bezpečné souběžné čtení, zatímco worker vlákno zapisuje
    return conn


def init_db(db_path: Path = DEFAULT_DB_PATH) -> None:
    conn = _connect(db_path)
    try:
        conn.executescript(_SCHEMA_SQL)
        conn.commit()
    finally:
        conn.close()
    reset_stale_running(db_path)


def reset_stale_running(db_path: Path = DEFAULT_DB_PATH) -> int:
    """Volá se při startu procesu (zadání bod 17 - "persistent cache
    survival"): žádný in-process `TerrainWorker` může na čerstvě
    nastartovaném procesu reálně mít rozpracovaný úkol - záznam se
    statusem `running` je proto vždy POZŮSTATEK po předchozím, zřejmě
    nedokončeném (spadlém) běhu procesu, ne stav, který by mohl
    dokončit sám od sebe. Smazání (ne jen přepsání na 'missing' - žádný
    takový stav v tabulce fyzicky neexistuje, "missing" = žádný řádek)
    ho nechá znovu zařadit při příštím requestu. Vrací počet smazaných řádků."""
    conn = _connect(db_path)
    try:
        cur = conn.execute("DELETE FROM terrain_cache WHERE status = ?", (STATUS_RUNNING,))
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def get_status(candidate_id: str, cache_key: str, db_path: Path = DEFAULT_DB_PATH) -> str:
    conn = _connect(db_path)
    try:
        row = conn.execute(
            "SELECT status FROM terrain_cache WHERE candidate_id = ? AND cache_key = ?", (candidate_id, cache_key)
        ).fetchone()
        return row["status"] if row else STATUS_MISSING
    finally:
        conn.close()


def mark_running(candidate_id: str, cache_key: str, lat: float, lon: float, db_path: Path = DEFAULT_DB_PATH) -> None:
    conn = _connect(db_path)
    try:
        conn.execute(
            "INSERT INTO terrain_cache (candidate_id, cache_key, status, lat, lon) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(candidate_id, cache_key) DO UPDATE SET status = excluded.status",
            (candidate_id, cache_key, STATUS_RUNNING, lat, lon),
        )
        conn.commit()
    finally:
        conn.close()


def save_ready(
    candidate_id: str,
    cache_key: str,
    lat: float,
    lon: float,
    static: StaticTerrain,
    compute_duration_s: float,
    db_path: Path = DEFAULT_DB_PATH,
    region: str | None = None,
    candidate_type: str | None = None,
) -> bool:
    """Uloží hotový statický výsledek jako 'ready'. Vrací True, pokud se
    skutečně zapsalo, False, pokud se zápis ZAMÍTL (viz níže).

    Milestone "DEM Quality and Resolution Control" - `cache_key` úmyslně
    NEOBSAHUJE DEM provider/rozlišení (viz `compute_cache_key()` výše -
    je to výsledek, ne vstupní parametr). To znamená, že bez další
    ochrany by opakované volání (typicky `--force` přepočet ve
    scripts/precompute_terrain.py, nebo teoreticky i live worker) mohlo
    TICHO přepsat existující kvalitnější záznam (např. ČÚZK 10m/px)
    horším (např. 36.1m/px, kvůli nedokumentovanému limitu ImageServeru
    - viz app/cuzk_dem.py effective_pixel_size()) - stejný cache_key, ale
    jiná skutečná kvalita dat. Bez zavádění nové dimenze klíče (což by
    vyžadovalo migraci existujících řádků) je nejbezpečnější ochrana
    porovnat `dem_resolution_m` PŘED přepsáním.

    DŮLEŽITÉ: porovnává se proti existujícímu `dem_resolution_m` BEZ
    OHLEDU na aktuální `status` řádku (ne jen `status == 'ready'`) -
    `mark_running()` (viz `TerrainWorker.enqueue()`) přepíše status na
    'running' TĖSNĖ PŘED tím, než se tahle funkce zavolá podruhé pro
    stejný (candidate_id, cache_key), ale `dem_resolution_m` ze starého
    'ready' řádku zůstává nedotčené (`mark_running` aktualizuje jen
    `status`). Kontrola jen na `status == 'ready'` by tedy u ŽIVÉHO
    workeru vždy prošla (status už by byl 'running'), a ochrana by byla
    fakticky nefunkční - proto se spoléhá čistě na to, že
    `dem_resolution_m IS NOT NULL` znamená "existuje dřívější skutečně
    dokončená analýza", bez ohledu na přechodný status."""
    conn = _connect(db_path)
    try:
        existing = conn.execute(
            "SELECT dem_resolution_m FROM terrain_cache WHERE candidate_id = ? AND cache_key = ?",
            (candidate_id, cache_key),
        ).fetchone()
        if (
            existing is not None
            and existing["dem_resolution_m"] is not None
            and static.dem.resolution_m > existing["dem_resolution_m"] * 1.001
        ):
            logger.warning(
                "Terrain cache: nový výsledek pro %s má horší rozlišení (%.1fm/px) než dříve uložená data "
                "(%.1fm/px) se stejným cache_key %s - zápis odmítnut, existující záznam zachován beze změny.",
                candidate_id, static.dem.resolution_m, existing["dem_resolution_m"], cache_key,
            )
            # Zamítnutý zápis nesmí nechat řádek "zaseknutý" ve stavu
            # 'running' (viz TerrainWorker.enqueue() -> mark_running(),
            # které status přepsalo TĖSNĖ před tímto voláním) - existující
            # data jsou kompletní a platná (jinak by tu `dem_resolution_m`
            # nebylo), takže status se vrátí na 'ready', odkud ho
            # `get_static_terrain()` zase uvidí.
            conn.execute(
                "UPDATE terrain_cache SET status = ? WHERE candidate_id = ? AND cache_key = ?",
                (STATUS_READY, candidate_id, cache_key),
            )
            conn.commit()
            return False
        conn.execute(
            """
            INSERT INTO terrain_cache (
                candidate_id, cache_key, status, lat, lon, region, candidate_type, elevation_m,
                dem_provider, dem_dataset, dem_resolution_m, analysis_range_m, dem_fallback_used, dem_fallback_reason,
                slope_deg, aspect_deg, azimuths_json, horizon_altitudes_json,
                min_horizon_deg, max_horizon_deg, static_warnings_json, error_message,
                compute_duration_s, computed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)
            ON CONFLICT(candidate_id, cache_key) DO UPDATE SET
                status=excluded.status, region=excluded.region, candidate_type=excluded.candidate_type,
                elevation_m=excluded.elevation_m, dem_provider=excluded.dem_provider,
                dem_dataset=excluded.dem_dataset, dem_resolution_m=excluded.dem_resolution_m,
                analysis_range_m=excluded.analysis_range_m,
                dem_fallback_used=excluded.dem_fallback_used, dem_fallback_reason=excluded.dem_fallback_reason,
                slope_deg=excluded.slope_deg, aspect_deg=excluded.aspect_deg,
                azimuths_json=excluded.azimuths_json, horizon_altitudes_json=excluded.horizon_altitudes_json,
                min_horizon_deg=excluded.min_horizon_deg, max_horizon_deg=excluded.max_horizon_deg,
                static_warnings_json=excluded.static_warnings_json, error_message=NULL,
                compute_duration_s=excluded.compute_duration_s, computed_at=excluded.computed_at
            """,
            (
                candidate_id,
                cache_key,
                STATUS_READY,
                lat,
                lon,
                region,
                candidate_type,
                static.location.elevation_m,
                static.dem.provider,
                static.dem.dataset,
                static.dem.resolution_m,
                static.dem.analysis_range_m,
                int(static.dem.fallback_used),
                static.dem.fallback_reason,
                static.terrain.slope_deg,
                static.terrain.aspect_deg,
                json.dumps(static.terrain.azimuths_deg),
                json.dumps(static.terrain.horizon_altitudes_deg),
                static.terrain.minimum_horizon_deg,
                static.terrain.maximum_horizon_deg,
                json.dumps(static.warnings),
                compute_duration_s,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        conn.commit()
        return True
    finally:
        conn.close()


def save_failed(candidate_id: str, cache_key: str, lat: float, lon: float, error_message: str, db_path: Path = DEFAULT_DB_PATH) -> None:
    conn = _connect(db_path)
    try:
        conn.execute(
            "INSERT INTO terrain_cache (candidate_id, cache_key, status, lat, lon, error_message, computed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(candidate_id, cache_key) DO UPDATE SET status=excluded.status, error_message=excluded.error_message, computed_at=excluded.computed_at",
            (candidate_id, cache_key, STATUS_FAILED, lat, lon, error_message, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
    finally:
        conn.close()


def get_static_terrain(candidate_id: str, cache_key: str, db_path: Path = DEFAULT_DB_PATH) -> StaticTerrain | None:
    """Rekonstruuje `StaticTerrain` z cache záznamu - ekvivalentní tomu,
    co by vrátila čerstvá `compute_static_terrain()` (zadání bod 7 -
    "výsledek musí být ekvivalentní cold analysis"). Vrátí `None`, pokud
    záznam neexistuje nebo není `ready`."""
    conn = _connect(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM terrain_cache WHERE candidate_id = ? AND cache_key = ? AND status = ?",
            (candidate_id, cache_key, STATUS_READY),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None

    azimuths = json.loads(row["azimuths_json"])
    altitudes = json.loads(row["horizon_altitudes_json"])
    profile = HorizonProfile(azimuths_deg=np.array(azimuths), horizon_altitude_deg=np.array(altitudes))
    terrain = TerrainInfo(
        azimuths_deg=azimuths,
        horizon_altitudes_deg=altitudes,
        minimum_horizon_deg=row["min_horizon_deg"] if row["min_horizon_deg"] is not None else float("nan"),
        maximum_horizon_deg=row["max_horizon_deg"] if row["max_horizon_deg"] is not None else float("nan"),
        slope_deg=row["slope_deg"],
        aspect_deg=row["aspect_deg"],
        _profile=profile,
    )
    dem_info = DemInfo(
        provider=row["dem_provider"],
        dataset=row["dem_dataset"],
        resolution_m=row["dem_resolution_m"],
        analysis_range_m=row["analysis_range_m"],
        source_crs="",
        acquired_at=datetime.fromisoformat(row["computed_at"]) if row["computed_at"] else None,
        fallback_used=bool(row["dem_fallback_used"]),
        fallback_reason=row["dem_fallback_reason"],
        quality_flag="ok",
    )
    location = LocationInfo(latitude=row["lat"], longitude=row["lon"], elevation_m=row["elevation_m"])
    warnings = json.loads(row["static_warnings_json"]) if row["static_warnings_json"] else []
    return StaticTerrain(location=location, dem=dem_info, terrain=terrain, warnings=warnings)


@dataclass
class StatusCounts:
    missing: int = 0
    running: int = 0
    ready: int = 0
    failed: int = 0


def count_by_status(db_path: Path = DEFAULT_DB_PATH, cache_key: str | None = None) -> dict[str, int]:
    if not db_path.exists():
        return {"missing": 0, "running": 0, "ready": 0, "failed": 0}
    conn = _connect(db_path)
    try:
        if cache_key:
            rows = conn.execute("SELECT status, COUNT(*) as n FROM terrain_cache WHERE cache_key = ? GROUP BY status", (cache_key,)).fetchall()
        else:
            rows = conn.execute("SELECT status, COUNT(*) as n FROM terrain_cache GROUP BY status").fetchall()
        return {r["status"]: r["n"] for r in rows}
    finally:
        conn.close()


def count_ready_by_region_and_type(db_path: Path = DEFAULT_DB_PATH, cache_key: str | None = None) -> dict:
    if not db_path.exists():
        return {"by_region": {}, "by_type": {}, "total": 0}
    conn = _connect(db_path)
    try:
        where = "status = ?"
        params: list = [STATUS_READY]
        if cache_key:
            where += " AND cache_key = ?"
            params.append(cache_key)
        by_region = {
            r["region"]: r["n"]
            for r in conn.execute(f"SELECT region, COUNT(*) as n FROM terrain_cache WHERE {where} GROUP BY region", params).fetchall()
        }
        by_type = {
            r["candidate_type"]: r["n"]
            for r in conn.execute(f"SELECT candidate_type, COUNT(*) as n FROM terrain_cache WHERE {where} GROUP BY candidate_type", params).fetchall()
        }
        total = sum(by_region.values())
        return {"by_region": by_region, "by_type": by_type, "total": total}
    finally:
        conn.close()
