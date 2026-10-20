"""Milestone "Complete Product Build" (pokračování) - "Best Days This
Week" na place page (zadání bod 3). Pro KAŽDÝ dostupný den předpovědi
skutečně přepočítá: čas západu (jiný den = jiný azimut/čas, počítáno ze
STEJNÉHO cachovaného statického terénu, ne zkopírováno z dneška),
počasí NEJBLIŽŠÍ tomu konkrétnímu západu, a plný Sunset Score - nikdy
nekopíruje dnešní skóre na jiné dny.

Výkon (zadání bod 4 - "avoid redundant requests"): sunset časy pro
všechny dny se spočítají NEJDŘÍV (levné, žádná síť), pak se počasí pro
VŠECHNY dny stáhne JEDNÍM sdíleným Open-Meteo požadavkem
(`weather_cache.fetch_weather_batch_cached`) - ne jeden request na den."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app import weather_cache
from app.analyze_location import Diagnostics, LocationAnalysis, StaticTerrain, compute_dynamic_sunset
from app.solar import make_observer
from app.sunset_score import SunsetScoreResult, compute_full_sunset_score
from app.weather_provider import MAX_FORECAST_DAYS

DEFAULT_BEST_DAYS_COUNT = 7


@dataclass
class DayForecast:
    date_iso: str
    sunset_time: datetime
    score: SunsetScoreResult


def compute_best_days(
    static: StaticTerrain,
    tz_name: str,
    num_days: int = DEFAULT_BEST_DAYS_COUNT,
    today: datetime | None = None,
) -> list[DayForecast]:
    """`num_days` je omezené `MAX_FORECAST_DAYS` (Open-Meteo limit) - víc
    dní bychom stejně nemohli poctivě skórovat (žádné počasí k dispozici)."""
    tz = ZoneInfo(tz_name)
    base = today.astimezone(tz) if today is not None else datetime.now(tz)
    base_noon = base.replace(hour=12, minute=0, second=0, microsecond=0)
    num_days = max(1, min(num_days, MAX_FORECAST_DAYS))

    per_day_analysis: list[tuple[str, LocationAnalysis]] = []
    for i in range(num_days):
        day_noon = base_noon + timedelta(days=i)
        solar, visibility, dyn_warnings = compute_dynamic_sunset(static, day_noon)
        analysis = LocationAnalysis(
            location=static.location,
            dem=static.dem,
            terrain=static.terrain,
            solar=solar,
            visibility=visibility,
            diagnostics=Diagnostics(warnings=static.warnings + dyn_warnings, calculation_parameters={}),
        )
        per_day_analysis.append((day_noon.date().isoformat(), analysis))

    sunset_times = [analysis.solar.astronomical_sunset for _, analysis in per_day_analysis]
    try:
        weather_by_time = weather_cache.fetch_weather_batch_cached(
            static.location.latitude, static.location.longitude, sunset_times, timezone_name=tz_name,
        )
    except Exception:
        weather_by_time = {}  # počasí nedostupné pro celý týden -> každý den se poctivě přepočítá bez něj (reweight), ne fake

    observer = make_observer(static.location.latitude, static.location.longitude, static.location.elevation_m)
    results: list[DayForecast] = []
    for date_iso, analysis in per_day_analysis:
        weather = weather_by_time.get(analysis.solar.astronomical_sunset)
        score = compute_full_sunset_score(
            analysis, weather, observer, analysis.solar.astronomical_sunset, static.location.elevation_m,
        )
        results.append(DayForecast(date_iso=date_iso, sunset_time=analysis.solar.astronomical_sunset, score=score))
    return results
