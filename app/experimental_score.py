"""Krok 10: PRVNÍ EXPERIMENTÁLNÍ geometrický indikátor "otevřenosti"
západu - NENÍ to AI skóre, NENÍ to skóre "kvality" (barevnost, počasí...).
Je to čistě deterministická kombinace tří už vypočtených geometrických
veličin z app/horizon.py a app/visibility.py. Označit jako experimentální
kdekoliv se zobrazí uživateli - vzorec i váhy se budou měnit."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from app.horizon import HorizonProfile
from app.visibility import SunsetVisibilityResult

# Váhy jsou vědomě "od oka", ne kalibrované na datech - proto experimentální.
MAX_MEANINGFUL_DELAY_MINUTES = 60.0  # delší zpoždění už skóre neposílá dál k 0


@dataclass
class ExperimentalSunsetOpenness:
    score_0_100: float
    delay_minutes: float
    western_sector_open_fraction: float  # podíl západního sektoru pod astronomickým horizontem (0°)
    western_sector_avg_altitude_deg: float
    is_experimental: bool = True


def compute_experimental_openness(
    profile: HorizonProfile,
    visibility: SunsetVisibilityResult,
    sector_start_deg: float = 240.0,
    sector_end_deg: float = 300.0,
) -> ExperimentalSunsetOpenness:
    mask = (profile.azimuths_deg >= sector_start_deg) & (profile.azimuths_deg <= sector_end_deg)
    sector_values = profile.horizon_altitude_deg[mask]
    sector_values = sector_values[~np.isnan(sector_values)]

    if len(sector_values) == 0:
        open_fraction = 0.0
        avg_altitude = float("nan")
    else:
        open_fraction = float((sector_values < 0).sum() / len(sector_values))
        avg_altitude = float(sector_values.mean())

    delay_minutes = 0.0
    if visibility.terrain_obstructed:
        delay_minutes = (visibility.astronomical_sunset_time - visibility.visible_until_time).total_seconds() / 60.0

    delay_penalty = min(delay_minutes / MAX_MEANINGFUL_DELAY_MINUTES, 1.0)  # 0 (žádné zpoždění) .. 1 (max penalizace)

    # Kombinace: 50 % váha na tom, jak moc otevřený je celý západní sektor
    # (ne jen přesný azimut západu), 50 % na skutečném časovém zpoždění.
    score = 100.0 * (0.5 * open_fraction + 0.5 * (1.0 - delay_penalty))

    return ExperimentalSunsetOpenness(
        score_0_100=round(score, 1),
        delay_minutes=delay_minutes,
        western_sector_open_fraction=open_fraction,
        western_sector_avg_altitude_deg=avg_altitude,
    )
