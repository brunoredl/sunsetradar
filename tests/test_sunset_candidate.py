"""Testy pro app/sunset_candidate.py (Fáze 6 - datový model, ŽÁDNÉ
vyhledávání) - ověřuje jen převod LocationAnalysis -> SunsetCandidate a
konzistenci experimentálního geometry score s dřívějším Fáze 3 vzorem."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

from app.analyze_location import (
    DemInfo,
    Diagnostics,
    LocationAnalysis,
    LocationInfo,
    SolarInfo,
    TerrainInfo,
    VisibilityInfo,
)
from app.horizon import HorizonProfile
from app.sunset_candidate import GeometryScore, SunsetCandidate, compute_geometry_score


def _make_analysis(horizon_value_west: float, delay_minutes: float, obstructed: bool) -> LocationAnalysis:
    azimuths = np.arange(0, 360, 1.0)
    altitudes = np.where((azimuths >= 240) & (azimuths <= 300), horizon_value_west, -5.0)
    profile = HorizonProfile(azimuths_deg=azimuths, horizon_altitude_deg=altitudes)

    sunset = datetime(2026, 6, 21, 19, 20, 0, tzinfo=timezone.utc)
    terrain_obscured = sunset - timedelta(minutes=delay_minutes) if obstructed else sunset

    return LocationAnalysis(
        location=LocationInfo(latitude=50.0, longitude=14.0, elevation_m=500.0),
        dem=DemInfo(
            provider="cuzk_dmr5g", dataset="ČÚZK DMR 5G (DTM)", resolution_m=10.0, analysis_range_m=50000.0,
            source_crs="EPSG:4326", acquired_at=sunset, fallback_used=False, fallback_reason=None, quality_flag="ok",
        ),
        terrain=TerrainInfo(
            azimuths_deg=[float(a) for a in azimuths], horizon_altitudes_deg=[float(a) for a in altitudes],
            minimum_horizon_deg=float(altitudes.min()), maximum_horizon_deg=float(altitudes.max()),
            slope_deg=5.0, aspect_deg=180.0, _profile=profile,
        ),
        solar=SolarInfo(
            query_time=sunset, current_azimuth_deg=150.0, current_altitude_deg=40.0,
            astronomical_sunset=sunset, sunset_azimuth_deg=272.0, sunset_terrain_horizon_deg=horizon_value_west,
            astronomical_sunrise=None, sunrise_azimuth_deg=None, sunrise_terrain_horizon_deg=None,
        ),
        visibility=VisibilityInfo(
            terrain_obstruction=obstructed, astronomical_sunset=sunset, terrain_obscured_sunset=terrain_obscured,
            delay_minutes=delay_minutes if obstructed else 0.0, visibility_duration=timedelta(hours=2),
            visibility_duration_is_lower_bound=False, sunrise_terrain_obstruction=None,
            terrain_revealed_sunrise=None, sunrise_delay_minutes=None,
        ),
        diagnostics=Diagnostics(warnings=[], calculation_parameters={}),
    )


def test_from_location_analysis_maps_fields_correctly():
    analysis = _make_analysis(horizon_value_west=-2.0, delay_minutes=5.0, obstructed=True)
    candidate = SunsetCandidate.from_location_analysis(analysis)

    assert candidate.latitude == 50.0
    assert candidate.longitude == 14.0
    assert candidate.elevation_m == 500.0
    assert candidate.terrain_obstruction is True
    assert candidate.terrain_delay_minutes == 5.0
    assert candidate.dem_provider == "cuzk_dmr5g"
    assert candidate.visibility_duration_minutes == pytest.approx(120.0)


def test_future_weather_fields_default_to_none():
    analysis = _make_analysis(horizon_value_west=-2.0, delay_minutes=0.0, obstructed=False)
    candidate = SunsetCandidate.from_location_analysis(analysis)

    assert candidate.cloud_cover_pct is None
    assert candidate.cloud_base_m is None
    assert candidate.visibility_km is None
    assert candidate.precipitation_probability_pct is None
    assert candidate.wind_speed_ms is None


def test_open_horizon_scores_higher_than_obstructed():
    open_analysis = _make_analysis(horizon_value_west=-3.0, delay_minutes=0.0, obstructed=False)
    blocked_analysis = _make_analysis(horizon_value_west=20.0, delay_minutes=60.0, obstructed=True)

    open_score = compute_geometry_score(open_analysis)
    blocked_score = compute_geometry_score(blocked_analysis)

    assert isinstance(open_score, GeometryScore)
    assert open_score.is_experimental is True
    assert open_score.score_0_100 > blocked_score.score_0_100
    assert open_score.score_0_100 > 90
    assert blocked_score.score_0_100 < 10


def test_geometry_score_is_bounded_0_100():
    for horizon_val, delay, obstructed in [(-10, 0, False), (30, 200, True), (0, 30, True)]:
        analysis = _make_analysis(horizon_val, delay, obstructed)
        score = compute_geometry_score(analysis)
        assert 0.0 <= score.score_0_100 <= 100.0


def test_western_sector_open_fraction_reflects_horizon_shape():
    fully_open = _make_analysis(horizon_value_west=-5.0, delay_minutes=0.0, obstructed=False)
    fully_blocked = _make_analysis(horizon_value_west=15.0, delay_minutes=10.0, obstructed=True)

    assert compute_geometry_score(fully_open).western_sector_open_fraction == 1.0
    assert compute_geometry_score(fully_blocked).western_sector_open_fraction == 0.0
