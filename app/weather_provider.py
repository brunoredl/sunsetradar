"""Milestone "Complete Product Build" - reálné počasí přes Open-Meteo
(https://open-meteo.com/en/docs) - zdarma, BEZ API klíče, BEZ účtu.

AUDIT PŘED IMPLEMENTACÍ (živě ověřeno, ne jen z dokumentace): dotaz na
`/v1/forecast` s `hourly=cloud_cover,cloud_cover_low,...` bez jakékoliv
autentizace vrátil skutečná, vyplněná hodinová data (ne null) pro
libovolný bod v ČR. Usage policy (open-meteo.com/en/features#pricing):
nekomerční/osobní použití zdarma, fair-use limit ~10 000 requestů/den -
pro lokální prototyp s desítkami kandidátů denně naprosto dostatečné,
zejména s cachí (viz app/weather_cache.py). `forecast_days` až 16 dní
dopředu - pokrývá "Best days this week" (zadání bod 25).

ŽÁDNÁ FAKE DATA: pokud Open-Meteo nevrátí hodinu blízkou požadovanému
`target_time`, nebo request selže, `fetch_weather_forecast()` vyhodí
`WeatherUnavailableError` - volající (viz app/weather_score.py) MUSÍ
tenhle stav zobrazit jako "unavailable", nikdy nedopočítat fake
hodnotu."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

import requests

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
DEFAULT_TIMEOUT_S = 15
MAX_FORECAST_DAYS = 16  # Open-Meteo limit pro free tier bez klíče

HOURLY_FIELDS = (
    "cloud_cover,cloud_cover_low,cloud_cover_mid,cloud_cover_high,visibility,"
    "precipitation,precipitation_probability,temperature_2m,wind_speed_10m,"
    "wind_direction_10m,relative_humidity_2m,surface_pressure,weather_code"
)

# Zjednodušený převod WMO weather_code (https://open-meteo.com/en/docs -
# "WMO Weather interpretation codes") na krátký lidsky čitelný popis -
# jen kódy, které se v ČR reálně objevují, ne celá tabulka.
_WMO_LABELS = {
    0: "clear sky", 1: "mainly clear", 2: "partly cloudy", 3: "overcast",
    45: "fog", 48: "depositing rime fog",
    51: "light drizzle", 53: "moderate drizzle", 55: "dense drizzle",
    61: "slight rain", 63: "moderate rain", 65: "heavy rain",
    71: "slight snow", 73: "moderate snow", 75: "heavy snow",
    80: "rain showers", 81: "moderate rain showers", 82: "violent rain showers",
    95: "thunderstorm",
}


class WeatherUnavailableError(RuntimeError):
    """Počasí se nepodařilo získat (síť/timeout/mimo dosah předpovědi) -
    volající NIKDY nesmí tohle potichu nahradit vymyšlenou hodnotou."""


@dataclass
class WeatherInfo:
    latitude: float
    longitude: float
    forecast_time: datetime  # skutečná hodina z Open-Meteo nejbližší dotazovanému targetu
    cloud_cover_pct: float
    cloud_cover_low_pct: float
    cloud_cover_mid_pct: float
    cloud_cover_high_pct: float
    visibility_m: float | None
    precipitation_probability_pct: float | None
    precipitation_mm: float
    temperature_c: float
    wind_speed_kmh: float
    wind_direction_deg: float
    humidity_pct: float
    pressure_hpa: float | None
    weather_code: int
    weather_condition: str
    provider: str = "open-meteo"
    retrieved_at: datetime | None = None


def _fetch_raw_hourly(lat: float, lon: float, forecast_days: int, timezone_name: str) -> dict:
    try:
        response = requests.get(
            OPEN_METEO_URL,
            params={
                "latitude": lat,
                "longitude": lon,
                "hourly": HOURLY_FIELDS,
                "timezone": timezone_name,
                "forecast_days": forecast_days,
            },
            timeout=DEFAULT_TIMEOUT_S,
        )
        response.raise_for_status()
        data = response.json()
    except requests.exceptions.RequestException as exc:
        raise WeatherUnavailableError(f"Open-Meteo dotaz selhal: {exc}") from exc

    hourly = data.get("hourly")
    if not hourly or not hourly.get("time"):
        raise WeatherUnavailableError("Open-Meteo vrátilo prázdnou předpověď.")
    return hourly


def _weather_info_at_index(hourly: dict, idx: int, lat: float, lon: float) -> WeatherInfo:
    def val(field: str):
        return hourly.get(field, [None] * len(hourly["time"]))[idx]

    code = int(val("weather_code")) if val("weather_code") is not None else -1
    return WeatherInfo(
        latitude=lat,
        longitude=lon,
        forecast_time=datetime.fromisoformat(hourly["time"][idx]),
        cloud_cover_pct=_f(val("cloud_cover")),
        cloud_cover_low_pct=_f(val("cloud_cover_low")),
        cloud_cover_mid_pct=_f(val("cloud_cover_mid")),
        cloud_cover_high_pct=_f(val("cloud_cover_high")),
        visibility_m=val("visibility"),
        precipitation_probability_pct=val("precipitation_probability"),
        precipitation_mm=_f(val("precipitation")),
        temperature_c=_f(val("temperature_2m")),
        wind_speed_kmh=_f(val("wind_speed_10m")),
        wind_direction_deg=_f(val("wind_direction_10m")),
        humidity_pct=_f(val("relative_humidity_2m")),
        pressure_hpa=val("surface_pressure"),
        weather_code=code,
        weather_condition=_WMO_LABELS.get(code, "unknown"),
        retrieved_at=datetime.now(timezone.utc),
    )


def _forecast_days_for(days_ahead: int) -> int:
    return max(1, min(MAX_FORECAST_DAYS, days_ahead + 2))


def _days_ahead(target_time: datetime) -> int:
    if target_time.tzinfo is None:
        raise ValueError("target_time musí být timezone-aware")
    now = datetime.now(timezone.utc)
    return (target_time.astimezone(timezone.utc).date() - now.date()).days


def fetch_weather_forecast(lat: float, lon: float, target_time: datetime, timezone_name: str = "UTC") -> WeatherInfo:
    """Dotáže Open-Meteo pro danou polohu a najde hodinový záznam
    NEJBLIŽŠÍ `target_time` (typicky čas západu Slunce - zadání bod 27,
    "weather nearest to sunset", ne aktuální počasí)."""
    days_ahead = _days_ahead(target_time)
    if days_ahead < 0 or days_ahead > MAX_FORECAST_DAYS:
        raise WeatherUnavailableError(
            f"target_time {target_time.isoformat()} je mimo dosah Open-Meteo předpovědi (max {MAX_FORECAST_DAYS} dní dopředu)."
        )
    hourly = _fetch_raw_hourly(lat, lon, _forecast_days_for(days_ahead), timezone_name)
    idx = _closest_hour_index(hourly["time"], target_time, timezone_name)
    if idx is None:
        raise WeatherUnavailableError(f"Žádná hodina předpovědi blízko {target_time.isoformat()}.")
    return _weather_info_at_index(hourly, idx, lat, lon)


def fetch_weather_forecast_batch(
    lat: float, lon: float, target_times: list[datetime], timezone_name: str = "UTC"
) -> dict[datetime, WeatherInfo]:
    """Milestone "Complete Product Build" (pokračování) - "Best days this
    week" (zadání bod 3) potřebuje počasí pro až 7+ různých dnů u
    STEJNÉHO kandidáta. Naivní řešení by zavolalo `fetch_weather_forecast()`
    jednou na den (N requestů); tahle verze natáhne JEDNU odpověď, která
    pokrývá nejvzdálenější požadovaný den, a najde nejbližší hodinu pro
    KAŽDÝ `target_time` v ní - N samostatných HTTP requestů na jeden
    kandidát dolů na jeden (zadání bod 4 - "avoid redundant requests")."""
    if not target_times:
        return {}
    days_ahead_list = [_days_ahead(t) for t in target_times]
    if min(days_ahead_list) < 0 or max(days_ahead_list) > MAX_FORECAST_DAYS:
        raise WeatherUnavailableError(
            f"jeden z požadovaných dnů je mimo dosah Open-Meteo předpovědi (max {MAX_FORECAST_DAYS} dní dopředu)."
        )
    hourly = _fetch_raw_hourly(lat, lon, _forecast_days_for(max(days_ahead_list)), timezone_name)
    result: dict[datetime, WeatherInfo] = {}
    for target_time in target_times:
        idx = _closest_hour_index(hourly["time"], target_time, timezone_name)
        if idx is not None:
            result[target_time] = _weather_info_at_index(hourly, idx, lat, lon)
    return result


def _f(v) -> float:
    return float(v) if v is not None else float("nan")


def _closest_hour_index(times: list[str], target_time: datetime, timezone_name: str) -> int | None:
    from zoneinfo import ZoneInfo

    tz = ZoneInfo(timezone_name) if timezone_name != "UTC" else timezone.utc
    target_local = target_time.astimezone(tz).replace(tzinfo=None)
    best_idx, best_diff = None, None
    for i, t in enumerate(times):
        t_parsed = datetime.fromisoformat(t)
        diff = abs((t_parsed - target_local).total_seconds())
        if best_diff is None or diff < best_diff:
            best_idx, best_diff = i, diff
    # Víc než 90 minut od nejbližší dostupné hodiny už není "nejbližší
    # hodina k západu", je to prostě chybějící data.
    if best_diff is not None and best_diff <= 90 * 60:
        return best_idx
    return None
