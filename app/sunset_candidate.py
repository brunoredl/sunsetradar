"""Fáze 6 - PŘÍPRAVA (ne implementace) budoucí funkce "Find sunset spots".

Tenhle modul definuje jen:
1. `SunsetCandidate` - datový model jednoho místa jako kandidáta pro
   budoucí porovnávání (viz PLAN.md Fáze 6).
2. `compute_geometry_score()` - EXPERIMENTÁLNÍ čistě geometrická metrika,
   analogická `app.experimental_score` (Fáze 3), ale postavená nad
   veřejným výstupem `analyze_location()` místo nad nižšími komponentami.

ZÁMĚRNĚ NENÍ implementováno: žádné vyhledávání přes síť bodů, žádné
řazení/ranking, žádné "nejlepší místo". Jen datová struktura + jedna
technická metrika pro budoucí použití - viz zadání "neimplementuj zatím
plné vyhledávání... pouze navrhni... žádné subjektivní hodnocení"."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

import numpy as np

from app.analyze_location import LocationAnalysis

# Stejná konvence jako app.experimental_score (Fáze 3, Krok 10) -
# západní sektor pro "je horizont v týhle oblasti otevřený".
WESTERN_SECTOR_START_DEG = 240.0
WESTERN_SECTOR_END_DEG = 300.0
MAX_MEANINGFUL_DELAY_MINUTES = 60.0


@dataclass
class SunsetCandidate:
    """Jedno místo jako kandidát pro BUDOUCÍ porovnávání - postavené nad
    už hotovým `LocationAnalysis`, nic nepočítá samo. Pole `cloud_cover_pct`
    a další "budoucí" pole jsou záměrně `None` - weather vrstva (Fáze 6,
    bod 15) na tenhle model teprve naváže, `SunsetCandidate` sám o sobě
    na počasí NEZÁVISÍ a funguje bez něj."""

    latitude: float
    longitude: float
    elevation_m: float

    terrain_horizon_at_sunset_deg: float
    sunset_azimuth_deg: float
    terrain_obstruction: bool
    terrain_delay_minutes: float
    visibility_duration_minutes: float | None
    visibility_duration_is_lower_bound: bool

    dem_provider: str
    dem_resolution_m: float
    dem_quality_flag: str
    dem_fallback_used: bool

    analyzed_at: datetime

    # --- budoucí rozšíření (Fáze 6, bod 15) - zatím vždy None ---
    cloud_cover_pct: float | None = None
    cloud_base_m: float | None = None
    visibility_km: float | None = None
    precipitation_probability_pct: float | None = None
    wind_speed_ms: float | None = None

    @classmethod
    def from_location_analysis(cls, analysis: LocationAnalysis) -> "SunsetCandidate":
        duration = analysis.visibility.visibility_duration
        return cls(
            latitude=analysis.location.latitude,
            longitude=analysis.location.longitude,
            elevation_m=analysis.location.elevation_m,
            terrain_horizon_at_sunset_deg=analysis.solar.sunset_terrain_horizon_deg,
            sunset_azimuth_deg=analysis.solar.sunset_azimuth_deg,
            terrain_obstruction=analysis.visibility.terrain_obstruction,
            terrain_delay_minutes=analysis.visibility.delay_minutes,
            visibility_duration_minutes=(duration.total_seconds() / 60.0 if duration is not None else None),
            visibility_duration_is_lower_bound=analysis.visibility.visibility_duration_is_lower_bound,
            dem_provider=analysis.dem.provider,
            dem_resolution_m=analysis.dem.resolution_m,
            dem_quality_flag=analysis.dem.quality_flag,
            dem_fallback_used=analysis.dem.fallback_used,
            analyzed_at=analysis.dem.acquired_at or datetime.now(timezone.utc),
        )


@dataclass
class GeometryScore:
    score_0_100: float
    western_sector_open_fraction: float
    western_sector_avg_altitude_deg: float
    is_experimental: bool = True


def compute_geometry_score(analysis: LocationAnalysis) -> GeometryScore:
    """EXPERIMENTÁLNÍ (viz zadání bod 14) - kombinuje otevřenost západního
    sektoru + skutečné časové zpoždění západu, STEJNÝ princip jako
    `app.experimental_score.compute_experimental_openness` (Fáze 3), jen
    počítáno přímo nad veřejným `LocationAnalysis` místo nad nižšími
    komponentami (`HorizonProfile`/`SunsetVisibilityResult`) - proto
    samostatná (menší) implementace místo volání staré funkce s obalovými
    objekty. NENÍ to produkční skóre, NENÍ to hodnocení kvality/krásy
    západu - jen geometrická metrika pro budoucí srovnávání míst."""
    azimuths = np.array(analysis.terrain.azimuths_deg)
    altitudes = np.array(analysis.terrain.horizon_altitudes_deg)
    mask = (azimuths >= WESTERN_SECTOR_START_DEG) & (azimuths <= WESTERN_SECTOR_END_DEG)
    sector_values = altitudes[mask]
    sector_values = sector_values[~np.isnan(sector_values)]

    if len(sector_values) == 0:
        open_fraction = 0.0
        avg_altitude = float("nan")
    else:
        open_fraction = float((sector_values < 0).sum() / len(sector_values))
        avg_altitude = float(sector_values.mean())

    delay_penalty = min(analysis.visibility.delay_minutes / MAX_MEANINGFUL_DELAY_MINUTES, 1.0)
    score = 100.0 * (0.5 * open_fraction + 0.5 * (1.0 - delay_penalty))

    return GeometryScore(
        score_0_100=round(score, 1),
        western_sector_open_fraction=open_fraction,
        western_sector_avg_altitude_deg=avg_altitude,
    )
