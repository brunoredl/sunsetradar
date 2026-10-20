"""Milestone "Complete Product Build" - weather cache (zadání bod 7),
ÚMYSLNĚ oddělená od terrain cache (`app/terrain_cache.py`, samostatný
soubor `data/weather_cache.sqlite3`, ne stejná tabulka/DB). Terrain je
statický (cachuje se navěky, dokud se nezmění engine); počasí je
dynamické a potřebuje TTL - musí jít invalidovat/refreshnout bez
jakéhokoliv dopadu na terrain cache."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.weather_provider import WeatherInfo, fetch_weather_forecast, fetch_weather_forecast_batch

DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "data" / "weather_cache.sqlite3"

# Zadání bod 7 - "TTL". 3 hodiny: Open-Meteo model běhy se aktualizují
# řádově jednou za pár hodin, takže kratší TTL by jen zbytečně
# znásobovalo requesty bez reálného zisku přesnosti (fair-use k API,
# zadání bod 34).
DEFAULT_TTL = timedelta(hours=3)

# Zaokrouhlení souřadnic pro cache klíč (zadání "vhodně cachuj") -
# ~0.01° ≈ 1,1 km, srovnatelné s prostorovým rozlišením samotného
# Open-Meteo modelu - kandidáti blíž než tohle sdílejí jeden záznam
# místo zbytečně duplicitních requestů na prakticky stejné počasí.
COORD_ROUND_DECIMALS = 2

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS weather_cache (
    lat_key REAL NOT NULL,
    lon_key REAL NOT NULL,
    forecast_hour TEXT NOT NULL,
    provider TEXT NOT NULL,
    retrieved_at TEXT NOT NULL,
    latitude REAL NOT NULL,
    longitude REAL NOT NULL,
    forecast_time TEXT NOT NULL,
    cloud_cover_pct REAL,
    cloud_cover_low_pct REAL,
    cloud_cover_mid_pct REAL,
    cloud_cover_high_pct REAL,
    visibility_m REAL,
    precipitation_probability_pct REAL,
    precipitation_mm REAL,
    temperature_c REAL,
    wind_speed_kmh REAL,
    wind_direction_deg REAL,
    humidity_pct REAL,
    pressure_hpa REAL,
    weather_code INTEGER,
    weather_condition TEXT,
    PRIMARY KEY (lat_key, lon_key, forecast_hour, provider)
);
"""


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


def _cache_key(lat: float, lon: float, target_time: datetime) -> tuple[float, float, str]:
    lat_key = round(lat, COORD_ROUND_DECIMALS)
    lon_key = round(lon, COORD_ROUND_DECIMALS)
    # Zaokrouhleno na hodinu - Open-Meteo je stejně hodinová předpověď,
    # víc přesnosti v klíči by jen znásobovalo cache miss zbytečně.
    forecast_hour = target_time.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:00")
    return lat_key, lon_key, forecast_hour


def _row_to_weather(row: sqlite3.Row) -> WeatherInfo:
    return WeatherInfo(
        latitude=row["latitude"],
        longitude=row["longitude"],
        forecast_time=datetime.fromisoformat(row["forecast_time"]),
        cloud_cover_pct=row["cloud_cover_pct"],
        cloud_cover_low_pct=row["cloud_cover_low_pct"],
        cloud_cover_mid_pct=row["cloud_cover_mid_pct"],
        cloud_cover_high_pct=row["cloud_cover_high_pct"],
        visibility_m=row["visibility_m"],
        precipitation_probability_pct=row["precipitation_probability_pct"],
        precipitation_mm=row["precipitation_mm"],
        temperature_c=row["temperature_c"],
        wind_speed_kmh=row["wind_speed_kmh"],
        wind_direction_deg=row["wind_direction_deg"],
        humidity_pct=row["humidity_pct"],
        pressure_hpa=row["pressure_hpa"],
        weather_code=row["weather_code"],
        weather_condition=row["weather_condition"],
        provider=row["provider"],
        retrieved_at=datetime.fromisoformat(row["retrieved_at"]),
    )


def get_cached_weather(lat: float, lon: float, target_time: datetime, ttl: timedelta = DEFAULT_TTL, db_path: Path = DEFAULT_DB_PATH) -> WeatherInfo | None:
    """Vrátí cachovaný záznam, pokud existuje a je v rámci TTL - jinak
    None (volající pak zavolá `fetch_weather_forecast()` naživo)."""
    lat_key, lon_key, forecast_hour = _cache_key(lat, lon, target_time)
    conn = _connect(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM weather_cache WHERE lat_key = ? AND lon_key = ? AND forecast_hour = ? AND provider = 'open-meteo'",
            (lat_key, lon_key, forecast_hour),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    retrieved_at = datetime.fromisoformat(row["retrieved_at"])
    if datetime.now(timezone.utc) - retrieved_at > ttl:
        return None  # stale - volající to zpracuje jako cache miss, ne mrtvá data
    return _row_to_weather(row)


def save_weather(lat: float, lon: float, target_time: datetime, weather: WeatherInfo, db_path: Path = DEFAULT_DB_PATH) -> None:
    lat_key, lon_key, forecast_hour = _cache_key(lat, lon, target_time)
    conn = _connect(db_path)
    try:
        conn.execute(
            """
            INSERT INTO weather_cache (
                lat_key, lon_key, forecast_hour, provider, retrieved_at, latitude, longitude, forecast_time,
                cloud_cover_pct, cloud_cover_low_pct, cloud_cover_mid_pct, cloud_cover_high_pct, visibility_m,
                precipitation_probability_pct, precipitation_mm, temperature_c, wind_speed_kmh, wind_direction_deg,
                humidity_pct, pressure_hpa, weather_code, weather_condition
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(lat_key, lon_key, forecast_hour, provider) DO UPDATE SET
                retrieved_at=excluded.retrieved_at, latitude=excluded.latitude, longitude=excluded.longitude,
                forecast_time=excluded.forecast_time, cloud_cover_pct=excluded.cloud_cover_pct,
                cloud_cover_low_pct=excluded.cloud_cover_low_pct, cloud_cover_mid_pct=excluded.cloud_cover_mid_pct,
                cloud_cover_high_pct=excluded.cloud_cover_high_pct, visibility_m=excluded.visibility_m,
                precipitation_probability_pct=excluded.precipitation_probability_pct, precipitation_mm=excluded.precipitation_mm,
                temperature_c=excluded.temperature_c, wind_speed_kmh=excluded.wind_speed_kmh,
                wind_direction_deg=excluded.wind_direction_deg, humidity_pct=excluded.humidity_pct,
                pressure_hpa=excluded.pressure_hpa, weather_code=excluded.weather_code, weather_condition=excluded.weather_condition
            """,
            (
                lat_key, lon_key, forecast_hour, weather.provider,
                (weather.retrieved_at or datetime.now(timezone.utc)).isoformat(),
                lat, lon, weather.forecast_time.isoformat(),
                weather.cloud_cover_pct, weather.cloud_cover_low_pct, weather.cloud_cover_mid_pct, weather.cloud_cover_high_pct,
                weather.visibility_m, weather.precipitation_probability_pct, weather.precipitation_mm,
                weather.temperature_c, weather.wind_speed_kmh, weather.wind_direction_deg,
                weather.humidity_pct, weather.pressure_hpa, weather.weather_code, weather.weather_condition,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def fetch_weather_cached(lat: float, lon: float, target_time: datetime, timezone_name: str = "UTC", ttl: timedelta = DEFAULT_TTL, db_path: Path = DEFAULT_DB_PATH) -> WeatherInfo:
    """Cache-aware verze `fetch_weather_forecast()` - jediné místo, které
    by ostatní kód (scoring, API) měl volat (zadání bod 7 - "weather
    data musí být možné aktualizovat bez přepočtu terrainu" - tenhle
    lookup je zcela nezávislý na terrain_cache.py)."""
    cached = get_cached_weather(lat, lon, target_time, ttl=ttl, db_path=db_path)
    if cached is not None:
        return cached
    fresh = fetch_weather_forecast(lat, lon, target_time, timezone_name=timezone_name)
    save_weather(lat, lon, target_time, fresh, db_path=db_path)
    return fresh


def fetch_weather_batch_cached(
    lat: float,
    lon: float,
    target_times: list[datetime],
    timezone_name: str = "UTC",
    ttl: timedelta = DEFAULT_TTL,
    db_path: Path = DEFAULT_DB_PATH,
) -> dict[datetime, WeatherInfo]:
    """Cache-aware verze `fetch_weather_forecast_batch()` - "Best days this
    week" (zadání bod 3): pro každý den nejdřív zkusí cache (sdílenou s
    `fetch_weather_cached()` - stejný klíč), a JEN pro dny, co v cachi
    chybí, provede JEDEN sdílený Open-Meteo request (ne jeden na den)."""
    result: dict[datetime, WeatherInfo] = {}
    misses: list[datetime] = []
    for t in target_times:
        cached = get_cached_weather(lat, lon, t, ttl=ttl, db_path=db_path)
        if cached is not None:
            result[t] = cached
        else:
            misses.append(t)
    if misses:
        fresh = fetch_weather_forecast_batch(lat, lon, misses, timezone_name=timezone_name)
        for t, weather in fresh.items():
            save_weather(lat, lon, t, weather, db_path=db_path)
            result[t] = weather
    return result
