"""Testy pro app/best_days.py (milestone "Complete Product Build",
pokračování - "Best Days This Week" na place page, zadání bod 3/4).
Skutečný `compute_dynamic_sunset()`/`compute_full_sunset_score()` běží
nad FAKE statickým terénem (stejný vzor jako tests/test_terrain_cache.py) -
jen `weather_cache.fetch_weather_batch_cached()` je mockovaný (žádná
reálná síť v unit testech)."""

from __future__ import annotations

from datetime import datetime, timezone

import numpy as np
import pytest

from app.analyze_location import DemInfo, LocationInfo, StaticTerrain, TerrainInfo
from app.best_days import DEFAULT_BEST_DAYS_COUNT, compute_best_days
from app.horizon import HorizonProfile
from app.weather_provider import MAX_FORECAST_DAYS, WeatherInfo


def _fake_static(lat=49.5, lon=18.4, elevation_m=1300.0):
    azimuths = np.arange(0.0, 360.0, 1.0)
    altitudes = np.full_like(azimuths, -0.5)
    profile = HorizonProfile(azimuths_deg=azimuths, horizon_altitude_deg=altitudes)
    terrain = TerrainInfo(
        azimuths_deg=[float(a) for a in azimuths], horizon_altitudes_deg=[float(v) for v in altitudes],
        minimum_horizon_deg=-0.5, maximum_horizon_deg=-0.5, slope_deg=3.0, aspect_deg=270.0, _profile=profile,
    )
    dem = DemInfo(
        provider="cuzk_dmr5g", dataset="ČÚZK DMR 5G (DTM)", resolution_m=10.0, analysis_range_m=50000.0,
        source_crs="EPSG:4326", acquired_at=None, fallback_used=False, fallback_reason=None, quality_flag="ok",
    )
    location = LocationInfo(latitude=lat, longitude=lon, elevation_m=elevation_m)
    return StaticTerrain(location=location, dem=dem, terrain=terrain, warnings=[])


def _fake_weather(cloud=20.0):
    return WeatherInfo(
        latitude=49.5, longitude=18.4, forecast_time=datetime.now(timezone.utc),
        cloud_cover_pct=cloud, cloud_cover_low_pct=cloud, cloud_cover_mid_pct=cloud / 2, cloud_cover_high_pct=cloud / 2,
        visibility_m=35000.0, precipitation_probability_pct=0.0, precipitation_mm=0.0, temperature_c=15.0,
        wind_speed_kmh=8.0, wind_direction_deg=270.0, humidity_pct=55.0, pressure_hpa=1013.0,
        weather_code=1, weather_condition="mainly clear", retrieved_at=datetime.now(timezone.utc),
    )


def test_compute_best_days_returns_one_entry_per_day(monkeypatch):
    monkeypatch.setattr(
        "app.best_days.weather_cache.fetch_weather_batch_cached",
        lambda lat, lon, target_times, timezone_name="UTC": {t: _fake_weather() for t in target_times},
    )
    static = _fake_static()
    days = compute_best_days(static, "Europe/Prague", num_days=5)
    assert len(days) == 5
    dates = {d.date_iso for d in days}
    assert len(dates) == 5  # 5 RŮZNÝCH dat, ne stejný den pětkrát


def test_compute_best_days_uses_different_sunset_time_per_day(monkeypatch):
    monkeypatch.setattr(
        "app.best_days.weather_cache.fetch_weather_batch_cached",
        lambda lat, lon, target_times, timezone_name="UTC": {t: _fake_weather() for t in target_times},
    )
    static = _fake_static()
    days = compute_best_days(static, "Europe/Prague", num_days=6)
    sunset_times = {d.sunset_time for d in days}
    assert len(sunset_times) == 6  # každý den skutečně jiný čas západu (posun v čase o dny)


def test_compute_best_days_scores_are_independently_computed_not_copied(monkeypatch):
    # Různé počasí pro různé dny -> různé skóre (důkaz, že se skóre
    # NEKOPÍRUJE z prvního dne, ale skutečně přepočítává za den).
    def fake_batch(lat, lon, target_times, timezone_name="UTC"):
        result = {}
        for i, t in enumerate(sorted(target_times)):
            result[t] = _fake_weather(cloud=10.0 if i % 2 == 0 else 95.0)
        return result

    monkeypatch.setattr("app.best_days.weather_cache.fetch_weather_batch_cached", fake_batch)
    static = _fake_static()
    days = compute_best_days(static, "Europe/Prague", num_days=4)
    scores = [d.score.overall_0_100 for d in days]
    assert len(set(scores)) > 1  # ne všechny dny mají identické skóre


def test_compute_best_days_survives_total_weather_failure(monkeypatch):
    def fake_batch(lat, lon, target_times, timezone_name="UTC"):
        raise RuntimeError("Open-Meteo down")

    monkeypatch.setattr("app.best_days.weather_cache.fetch_weather_batch_cached", fake_batch)
    static = _fake_static()
    days = compute_best_days(static, "Europe/Prague", num_days=3)
    assert len(days) == 3
    for d in days:
        assert d.score.weather is None  # poctivě "nedostupné", request nespadl


def test_compute_best_days_caps_num_days_at_forecast_limit(monkeypatch):
    monkeypatch.setattr(
        "app.best_days.weather_cache.fetch_weather_batch_cached",
        lambda lat, lon, target_times, timezone_name="UTC": {t: _fake_weather() for t in target_times},
    )
    static = _fake_static()
    days = compute_best_days(static, "Europe/Prague", num_days=MAX_FORECAST_DAYS + 10)
    assert len(days) == MAX_FORECAST_DAYS


def test_default_best_days_count_is_reasonable_for_a_week_view():
    assert 5 <= DEFAULT_BEST_DAYS_COUNT <= 10
