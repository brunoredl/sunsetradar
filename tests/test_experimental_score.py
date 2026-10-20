"""Testy pro app/experimental_score.py - jen ověření, že extrémy dávají
smysl (otevřený sektor + žádné zpoždění -> vysoké skóre, uzavřený sektor
+ velké zpoždění -> nízké skóre). Nejde o kalibrovaný model, jen o
konzistenci vzorce."""

from __future__ import annotations

from datetime import datetime, timezone

import numpy as np

from app.experimental_score import compute_experimental_openness
from app.horizon import HorizonProfile
from app.visibility import SunsetVisibilityResult


def _visibility(obstructed: bool, delay_minutes: float = 0.0) -> SunsetVisibilityResult:
    sunset = datetime(2026, 9, 22, 17, 0, 0, tzinfo=timezone.utc)
    from datetime import timedelta

    return SunsetVisibilityResult(
        observer_lat=50.0,
        observer_lon=14.0,
        astronomical_sunset_time=sunset,
        astronomical_sunset_azimuth_deg=270.0,
        astronomical_sunset_sun_altitude_deg=0.0,
        terrain_horizon_at_sunset_azimuth_deg=0.0,
        terrain_obstructed=obstructed,
        visible_until_time=sunset - timedelta(minutes=delay_minutes) if obstructed else sunset,
    )


def test_open_sector_no_delay_scores_high():
    azimuths = np.arange(240, 301, 1.0)
    horizon = HorizonProfile(azimuths_deg=azimuths, horizon_altitude_deg=np.full_like(azimuths, -3.0))
    result = compute_experimental_openness(horizon, _visibility(obstructed=False))
    assert result.score_0_100 > 90
    assert result.is_experimental is True


def test_closed_sector_with_big_delay_scores_low():
    azimuths = np.arange(240, 301, 1.0)
    horizon = HorizonProfile(azimuths_deg=azimuths, horizon_altitude_deg=np.full_like(azimuths, 15.0))
    result = compute_experimental_openness(horizon, _visibility(obstructed=True, delay_minutes=90))
    assert result.score_0_100 < 20


def test_score_is_between_0_and_100():
    azimuths = np.arange(240, 301, 1.0)
    horizon = HorizonProfile(azimuths_deg=azimuths, horizon_altitude_deg=np.linspace(-5, 10, len(azimuths)))
    result = compute_experimental_openness(horizon, _visibility(obstructed=True, delay_minutes=20))
    assert 0 <= result.score_0_100 <= 100
