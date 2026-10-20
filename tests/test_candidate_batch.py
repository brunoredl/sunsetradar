"""Testy pro app/candidate_batch.py (Fáze 8) - napojení kandidátů na
existující analyze_location()/compute_sunset_rating(). `analyze_location`
je tu vždy mockovaný (monkeypatch) - tenhle modul netestuje terrain
engine samotný (to dělají jiné testy), jen orchestraci/ranking/chování
při selhání."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

import app.candidate_batch as candidate_batch
from app.analyze_location import (
    DemInfo,
    Diagnostics,
    LocationAnalysis,
    LocationInfo,
    SolarInfo,
    TerrainInfo,
    VisibilityInfo,
)
from app.candidate_discovery import Candidate
from app.horizon import HorizonProfile


def _make_analysis(lat: float, lon: float, horizon_west_deg: float) -> LocationAnalysis:
    azimuths = np.arange(0, 360, 1.0)
    altitudes = np.where((azimuths >= 240) & (azimuths <= 300), horizon_west_deg, -5.0)
    profile = HorizonProfile(azimuths_deg=azimuths, horizon_altitude_deg=altitudes)
    sunset = datetime(2026, 9, 24, 19, 0, 0, tzinfo=timezone.utc)
    return LocationAnalysis(
        location=LocationInfo(latitude=lat, longitude=lon, elevation_m=900.0),
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
            astronomical_sunset=sunset, sunset_azimuth_deg=271.0, sunset_terrain_horizon_deg=horizon_west_deg,
            astronomical_sunrise=None, sunrise_azimuth_deg=None, sunrise_terrain_horizon_deg=None,
        ),
        visibility=VisibilityInfo(
            terrain_obstruction=horizon_west_deg > 0, astronomical_sunset=sunset, terrain_obscured_sunset=sunset,
            delay_minutes=0.0, visibility_duration=timedelta(hours=2),
            visibility_duration_is_lower_bound=False, sunrise_terrain_obstruction=None,
            terrain_revealed_sunrise=None, sunrise_delay_minutes=None,
        ),
        diagnostics=Diagnostics(warnings=[], calculation_parameters={}),
    )


def _candidate(id_, name, lat=49.5, lon=18.4, elevation=1000.0):
    return Candidate(id=id_, name=name, lat=lat, lon=lon, candidate_type="peak", elevation_m=elevation)


def test_analyze_candidates_sequential_returns_ranked_list(monkeypatch):
    open_c = _candidate("a", "Otevřený", lat=49.5, lon=18.4)
    blocked_c = _candidate("b", "Blokovaný", lat=49.6, lon=18.5)

    def fake_analyze(lat, lon, when):
        is_open = lat == open_c.lat and lon == open_c.lon
        return _make_analysis(lat, lon, horizon_west_deg=-5.0 if is_open else 20.0)

    monkeypatch.setattr(candidate_batch, "analyze_location", fake_analyze)
    results = candidate_batch.analyze_candidates([open_c, blocked_c], datetime.now(timezone.utc))
    assert len(results) == 2
    assert all(isinstance(r, candidate_batch.RankedCandidate) for r in results)
    assert results[0].candidate is open_c
    assert results[0].rating.overall_0_100 is not None


def test_analyze_candidates_skips_failed_ones(monkeypatch):
    good = _candidate("a", "Funguje")
    bad = _candidate("b", "NODATA")

    # první kandidát uspěje, druhý selže - simulováno přes pořadí volání
    calls = {"n": 0}

    def fake_analyze_ordered(lat, lon, when):
        calls["n"] += 1
        if calls["n"] == 1:
            return _make_analysis(lat, lon, horizon_west_deg=-2.0)
        raise RuntimeError("NODATA na pozici pozorovatele")

    monkeypatch.setattr(candidate_batch, "analyze_location", fake_analyze_ordered)
    results = candidate_batch.analyze_candidates([good, bad], datetime.now(timezone.utc))
    assert len(results) == 1
    assert results[0].candidate is good


def test_analyze_candidates_empty_input(monkeypatch):
    monkeypatch.setattr(candidate_batch, "analyze_location", lambda *a, **k: (_ for _ in ()).throw(AssertionError("neměl se volat")))
    assert candidate_batch.analyze_candidates([], datetime.now(timezone.utc)) == []


def test_rank_by_rating_sorts_descending(monkeypatch):
    low = _candidate("a", "Nízké skóre")
    high = _candidate("b", "Vysoké skóre")

    calls = {"n": 0}

    def fake_analyze(lat, lon, when):
        calls["n"] += 1
        # první volání (low) dostane blokovaný horizont, druhé (high) otevřený
        return _make_analysis(lat, lon, horizon_west_deg=25.0 if calls["n"] == 1 else -8.0)

    monkeypatch.setattr(candidate_batch, "analyze_location", fake_analyze)
    results = candidate_batch.analyze_candidates([low, high], datetime.now(timezone.utc))
    ranked = candidate_batch.rank_by_rating(results)
    assert ranked[0].candidate is high
    assert ranked[0].rating.overall_0_100 >= ranked[1].rating.overall_0_100


def test_rank_by_rating_is_deterministic(monkeypatch):
    a, b = _candidate("a", "A"), _candidate("b", "B")
    monkeypatch.setattr(candidate_batch, "analyze_location", lambda lat, lon, when: _make_analysis(lat, lon, -3.0))
    results = candidate_batch.analyze_candidates([a, b], datetime.now(timezone.utc))
    r1 = [r.candidate.id for r in candidate_batch.rank_by_rating(results)]
    r2 = [r.candidate.id for r in candidate_batch.rank_by_rating(results)]
    assert r1 == r2
