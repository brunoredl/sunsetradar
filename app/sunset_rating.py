"""Fáze 6.6 - Sunset Rating: deterministická, vysvětlitelná metrika 0-100
TECHNICKÉ VHODNOSTI místa pro VIDITELNOST západu Slunce (zadání bod 6) -
NENÍ to subjektivní hodnocení krásy místa. K tomu chybí počasí/cloud
cover/visibility/fotografie/crowd data/přístupnost (zadání bod 6/17) -
tenhle rating je záměrně jen o terénní geometrii.

Staví na `app.sunset_candidate.compute_geometry_score()` (Fáze 6) - jeho
základní myšlenku (otevřenost západního sektoru + zpoždění) rozšiřuje o:

1. `sunset_terrain_horizon_deg` PŘESNĚ v azimutu západu (ne jen průměr
   sektoru) - přímý geometrický signál "jak moc terén blokuje výhled
   přesně tam, kam Slunce zapadá".
2. Sektor otevřenosti CENTROVANÝ na skutečný `sunset_azimuth_deg`, místo
   pevného pásma 240-300° (AUDIT NÁLEZ: `compute_geometry_score()` mělo
   fixní západní sektor, který sedí jen pro sunset blízko due-west -
   skutečný azimut západu se ale v průběhu roku mění o desítky stupňů,
   takže fixní pásmo může minout relevantní terén pro krajní letní/zimní
   azimuty. `compute_geometry_score()` samo se NEMĚNÍ - má vlastní
   zamčený test kontrakt - tohle je nová, přesnější metrika).
3. Délku viditelnosti (`visibility_duration`) - jak dlouho je Slunce nad
   lokálním horizontem vidět předtím, než zmizí.

`compute_geometry_score()` zůstává beze změny a dál slouží jako
EXPERIMENTÁLNÍ interní model (Fáze 6) - do UI se nikdy nedostane (viz
PLAN.md audit, zadání bod 8 validační fáze). `compute_sunset_rating()`
je nová funkce určená přímo k zobrazení v produktu.

ARCHITEKTURA PRO BUDOUCÍ WEATHER (zadání bod 18/19): `SunsetRating` má
`geometry_score` (vždy vyplněné) + `weather_score`/`visibility_score`/
`accessibility_score` (zatím vždy `None` - žádné počasí API se nevolá).
`overall_0_100` je zatím vždy odvozené jen z `geometry_score` - až
přibudou další skóre, `overall_0_100` se stane jejich váženou kombinací,
beze změny tvaru tohohle modelu."""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from app.analyze_location import LocationAnalysis

# --- Meze pro převod geometrických veličin na 0-100 komponenty. Zvoleno
# na základě reálně pozorovaných hodnot z validační fáze (Milešovka
# horizon ~-0.07°/delay 5.5 min -> "skoro otevřeno"; Krušné hory horizon
# ~8.8°/delay 47 min -> "výrazná překážka") - ne odhadnuto naslepo. ---
HORIZON_FULLY_OPEN_DEG = -3.0
HORIZON_FULLY_BLOCKED_DEG = 25.0
MAX_MEANINGFUL_DELAY_MINUTES = 60.0
SECTOR_HALF_WIDTH_DEG = 20.0
SECTOR_OPEN_THRESHOLD_DEG = 0.0
DURATION_REFERENCE_MINUTES = 60.0

WEIGHT_HORIZON_AT_AZIMUTH = 0.35
WEIGHT_DELAY = 0.25
WEIGHT_SECTOR_OPENNESS = 0.25
WEIGHT_DURATION = 0.15
assert abs(WEIGHT_HORIZON_AT_AZIMUTH + WEIGHT_DELAY + WEIGHT_SECTOR_OPENNESS + WEIGHT_DURATION - 1.0) < 1e-9

# Zadání bod 9 - popisky podle pásma skóre, žádné subjektivní "beautiful"/"best".
_LABEL_THRESHOLDS: list[tuple[float, str]] = [
    (90.0, "Excellent terrain visibility"),
    (75.0, "Very open horizon"),
    (50.0, "Partial obstruction"),
    (25.0, "Strong terrain obstruction"),
    (0.0, "Sunset largely obstructed"),
]

RATING_NOTE = (
    "Sunset Rating currently represents terrain geometry only "
    "(no weather, cloud cover, visibility or accessibility data yet)."
)


@dataclass
class SunsetRatingComponents:
    """Jednotlivé 0-100 dílčí skóre, ze kterých se `overall_0_100` skládá -
    aby rating byl VYSVĚTLITELNÝ (zadání bod 8), ne černá skříňka."""

    horizon_at_azimuth: float
    delay: float
    sector_openness: float
    duration: float
    sector_open_fraction: float
    sector_avg_altitude_deg: float


@dataclass
class SunsetRating:
    overall_0_100: int
    label: str

    # Zadání bod 18 - architektura pro budoucí weather vrstvu. `geometry_score`
    # je vždy vyplněné; ostatní tři jsou zatím vždy None (žádné API volání).
    geometry_score: float
    weather_score: float | None
    visibility_score: float | None
    accessibility_score: float | None

    components: SunsetRatingComponents
    note: str = RATING_NOTE


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


def _label_for(score: float) -> str:
    for threshold, label in _LABEL_THRESHOLDS:
        if score >= threshold:
            return label
    return _LABEL_THRESHOLDS[-1][1]


def _sector_openness(
    azimuths_deg: list[float], altitudes_deg: list[float], center_deg: float, half_width_deg: float, threshold_deg: float
) -> tuple[float, float]:
    """Frakce vzorků v okně `[center-half_width, center+half_width]` (se
    správným wraparoundem přes šev 359°/0°) s výškovým úhlem terénu POD
    `threshold_deg` (= otevřený horizont), plus průměrný výškový úhel
    v tomhle okně. Vrací (0.0, NaN), pokud okno nepokrývá žádný vzorek."""
    az = np.asarray(azimuths_deg)
    alt = np.asarray(altitudes_deg)
    signed_diff = (az - center_deg + 180.0) % 360.0 - 180.0
    mask = np.abs(signed_diff) <= half_width_deg
    values = alt[mask]
    values = values[~np.isnan(values)]
    if len(values) == 0:
        return 0.0, float("nan")
    open_fraction = float((values < threshold_deg).sum() / len(values))
    return open_fraction, float(values.mean())


def compute_sunset_rating(analysis: LocationAnalysis) -> SunsetRating:
    """Čistě geometrický, deterministický a reprodukovatelný rating -
    NIC tu nevolá síť ani neodhaduje data, která `LocationAnalysis`
    neposkytuje (zadání bod 7 - "NEVYMÝŠLEJ data")."""
    horizon_at_sunset = analysis.solar.sunset_terrain_horizon_deg
    horizon_component = 100.0 * _clamp01(
        (HORIZON_FULLY_BLOCKED_DEG - horizon_at_sunset) / (HORIZON_FULLY_BLOCKED_DEG - HORIZON_FULLY_OPEN_DEG)
    )

    delay_component = 100.0 * _clamp01(1.0 - analysis.visibility.delay_minutes / MAX_MEANINGFUL_DELAY_MINUTES)

    sector_fraction, sector_avg_alt = _sector_openness(
        analysis.terrain.azimuths_deg,
        analysis.terrain.horizon_altitudes_deg,
        analysis.solar.sunset_azimuth_deg,
        SECTOR_HALF_WIDTH_DEG,
        SECTOR_OPEN_THRESHOLD_DEG,
    )
    sector_component = 100.0 * sector_fraction

    duration = analysis.visibility.visibility_duration
    if duration is not None:
        duration_component = 100.0 * _clamp01((duration.total_seconds() / 60.0) / DURATION_REFERENCE_MINUTES)
    else:
        # Prakticky nenastává (analyze_location() duration vždy dopočítá),
        # ale typ dovoluje None - neutrální komponenta místo pádu/odhadu.
        duration_component = 50.0

    overall = (
        WEIGHT_HORIZON_AT_AZIMUTH * horizon_component
        + WEIGHT_DELAY * delay_component
        + WEIGHT_SECTOR_OPENNESS * sector_component
        + WEIGHT_DURATION * duration_component
    )
    overall = round(_clamp01(overall / 100.0) * 100.0)

    return SunsetRating(
        overall_0_100=int(overall),
        label=_label_for(overall),
        geometry_score=float(overall),
        weather_score=None,
        visibility_score=None,
        accessibility_score=None,
        components=SunsetRatingComponents(
            horizon_at_azimuth=round(horizon_component, 1),
            delay=round(delay_component, 1),
            sector_openness=round(sector_component, 1),
            duration=round(duration_component, 1),
            sector_open_fraction=round(sector_fraction, 3),
            sector_avg_altitude_deg=round(sector_avg_alt, 2) if not math.isnan(sector_avg_alt) else sector_avg_alt,
        ),
    )
