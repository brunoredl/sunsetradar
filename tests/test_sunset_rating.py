"""Testy pro app/sunset_rating.py (Fáze 6.6 - Sunset Rating).

Ověřuje: rating je vždy 0-100, deterministický, otevřený horizont skóruje
výš než blokovaný, rating reaguje na horizont PŘESNĚ v azimutu západu (ne
jen na sektorový průměr), sektor otevřenosti je centrovaný na SKUTEČNÝ
sunset_azimuth_deg (ne na pevné pásmo 240-300° jako starší experimentální
`compute_geometry_score()`), popisky odpovídají pásmům, a že `/api/analyze`
rating skutečně serializuje."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np
import pytest
from fastapi.testclient import TestClient

import app.main as main_module
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
from app.sunset_rating import SunsetRating, compute_sunset_rating

client = TestClient(main_module.app)


def _make_analysis(
    *,
    sunset_azimuth_deg: float = 272.0,
    horizon_at_sunset_deg: float = -2.0,
    delay_minutes: float = 0.0,
    obstructed: bool = False,
    duration_minutes: float = 120.0,
    open_sector_center_deg: float | None = None,
    open_sector_half_width_deg: float = 25.0,
    background_altitude_deg: float = 15.0,
) -> LocationAnalysis:
    """Postaví 360° horizont, kde je otevřený (background_altitude_deg
    hodnota, typicky vysoká = blokované) sektor kolem `open_sector_center_deg`
    nastaven na `horizon_at_sunset_deg` (otevřeno), zbytek na
    `background_altitude_deg` (blokováno) - umožňuje testovat, jestli
    rating skutečně reaguje na terén KOLEM SKUTEČNÉHO azimutu západu, ne
    kolem nějakého pevného pásma."""
    if open_sector_center_deg is None:
        open_sector_center_deg = sunset_azimuth_deg

    azimuths = np.arange(0, 360, 1.0)
    diff = (azimuths - open_sector_center_deg + 180.0) % 360.0 - 180.0
    altitudes = np.where(np.abs(diff) <= open_sector_half_width_deg, horizon_at_sunset_deg, background_altitude_deg)
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
            astronomical_sunset=sunset, sunset_azimuth_deg=sunset_azimuth_deg,
            sunset_terrain_horizon_deg=horizon_at_sunset_deg,
            astronomical_sunrise=None, sunrise_azimuth_deg=None, sunrise_terrain_horizon_deg=None,
        ),
        visibility=VisibilityInfo(
            terrain_obstruction=obstructed, astronomical_sunset=sunset, terrain_obscured_sunset=terrain_obscured,
            delay_minutes=delay_minutes if obstructed else 0.0,
            visibility_duration=timedelta(minutes=duration_minutes),
            visibility_duration_is_lower_bound=False, sunrise_terrain_obstruction=None,
            terrain_revealed_sunrise=None, sunrise_delay_minutes=None,
        ),
        diagnostics=Diagnostics(warnings=[], calculation_parameters={}),
    )


def test_rating_is_bounded_0_100():
    cases = [
        dict(horizon_at_sunset_deg=-10, delay_minutes=0, obstructed=False, duration_minutes=180),
        dict(horizon_at_sunset_deg=40, delay_minutes=200, obstructed=True, duration_minutes=0),
        dict(horizon_at_sunset_deg=5, delay_minutes=30, obstructed=True, duration_minutes=45),
    ]
    for kwargs in cases:
        rating = compute_sunset_rating(_make_analysis(**kwargs))
        assert 0 <= rating.overall_0_100 <= 100
        assert isinstance(rating, SunsetRating)


def test_rating_is_deterministic():
    analysis = _make_analysis(horizon_at_sunset_deg=3.0, delay_minutes=12.0, obstructed=True)
    r1 = compute_sunset_rating(analysis)
    r2 = compute_sunset_rating(analysis)
    assert r1.overall_0_100 == r2.overall_0_100
    assert r1.label == r2.label
    assert r1.components == r2.components


def test_open_horizon_scores_much_higher_than_obstructed():
    open_analysis = _make_analysis(horizon_at_sunset_deg=-5.0, delay_minutes=0.0, obstructed=False, duration_minutes=180)
    blocked_analysis = _make_analysis(horizon_at_sunset_deg=25.0, delay_minutes=60.0, obstructed=True, duration_minutes=5)

    open_rating = compute_sunset_rating(open_analysis)
    blocked_rating = compute_sunset_rating(blocked_analysis)

    assert open_rating.overall_0_100 > blocked_rating.overall_0_100
    assert open_rating.overall_0_100 >= 90
    assert blocked_rating.overall_0_100 <= 10


def test_rating_reacts_to_horizon_exactly_at_sunset_azimuth():
    """Změna JEN `sunset_terrain_horizon_deg` (bez změny sektoru) musí
    změnit rating - ne jen sektorový průměr, jak to dělalo staré skóre."""
    low = _make_analysis(horizon_at_sunset_deg=-5.0, background_altitude_deg=-5.0)
    high = _make_analysis(horizon_at_sunset_deg=20.0, background_altitude_deg=-5.0)

    rating_low = compute_sunset_rating(low)
    rating_high = compute_sunset_rating(high)
    assert rating_low.components.horizon_at_azimuth > rating_high.components.horizon_at_azimuth
    assert rating_low.overall_0_100 > rating_high.overall_0_100


def test_sector_centers_on_actual_sunset_azimuth_not_a_fixed_window():
    """AUDIT NÁLEZ: starší `compute_geometry_score()` mělo pevné západní
    pásmo 240-300°. Pro sunset azimuth mimo tohle pásmo (např. 230° -
    reálně možné v zimě na vyšší zeměpisné šířce) by fixní pásmo minulo
    relevantní terén. Nový rating musí sektor centrovat na SKUTEČNÝ
    azimut, i když leží mimo 240-300°."""
    # Otevřeno kolem 230° (mimo staré fixní pásmo), zbytek terénu
    # (včetně celého 240-300°) silně blokovaný.
    analysis = _make_analysis(
        sunset_azimuth_deg=230.0,
        horizon_at_sunset_deg=-8.0,
        open_sector_center_deg=230.0,
        open_sector_half_width_deg=25.0,  # sirsi nez modulovo SECTOR_HALF_WIDTH_DEG (20), aby cely vzorkovaci okno bylo otevrene
        background_altitude_deg=30.0,
        delay_minutes=0.0,
        obstructed=False,
    )
    rating = compute_sunset_rating(analysis)
    # Sektor kolem 230° je otevřený -> vysoká sector_openness komponenta,
    # navzdory tomu, že staré fixní pásmo 240-300° by tu bylo celé blokované.
    assert rating.components.sector_openness > 80.0
    assert rating.overall_0_100 >= 85


@pytest.mark.parametrize(
    "score,expected_label",
    [(95, "Excellent terrain visibility"), (80, "Very open horizon"), (60, "Partial obstruction"),
     (30, "Strong terrain obstruction"), (5, "Sunset largely obstructed")],
)
def test_label_thresholds(score, expected_label):
    from app.sunset_rating import _label_for

    assert _label_for(score) == expected_label


def test_future_score_fields_default_to_none_with_explanatory_note():
    rating = compute_sunset_rating(_make_analysis())
    assert rating.weather_score is None
    assert rating.visibility_score is None
    assert rating.accessibility_score is None
    assert "geometry" in rating.note.lower()
    assert "weather" in rating.note.lower()


def test_api_response_includes_rating(monkeypatch):
    def fake_analyze(lat, lon, when):
        return _make_analysis(horizon_at_sunset_deg=-4.0, delay_minutes=2.0, obstructed=True, duration_minutes=150)

    monkeypatch.setattr(main_module, "analyze_location", fake_analyze)
    r = client.post("/api/analyze", json={"lat": 50.0, "lon": 14.0, "date": "2026-06-21", "timezone": "UTC"})
    assert r.status_code == 200
    data = r.json()
    assert "rating" in data
    assert 0 <= data["rating"]["overall_0_100"] <= 100
    assert isinstance(data["rating"]["label"], str)
    assert data["rating"]["weather_score"] is None
    assert "components" in data["rating"]
