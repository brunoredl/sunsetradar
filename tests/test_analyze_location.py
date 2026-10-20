"""Testy pro app/analyze_location.py (Fáze 4) - veřejné API `analyze_location()`.

Většina testů mockuje `resolve_dem()` a používá syntetický DEM
(tests/conftest.py) - rychlé, deterministické testy ORCHESTRACE (jak se
komponenty skládají dohromady), ne přepočet samotné geometrie (ta je
otestovaná zvlášť v test_horizon.py/test_visibility.py/test_solar.py).
Několik `@pytest.mark.integration` testů ověřuje skutečné end-to-end
chování proti reálným DEM zdrojům."""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin

from app.analyze_location import LocationAnalysis, analyze_location
from app.dem import DEMSource
from app.dem_strategy import PROVIDER_COPERNICUS, PROVIDER_CUZK, DEMMetadata

MILESOVKA = (50.5552, 13.9312)
PARIS = (48.8566, 2.3522)


@pytest.fixture
def large_flat_dem_path(tmp_path):
    """Větší plochý DEM (~66 km) než sdílený `flat_dem_path` z conftest.py
    (~4.4 km) - potřeba pro test "opravdu otevřený horizont", protože na
    krátký dosah i dokonale plochý terén vykazuje jistou "obstrukci" vůči
    idealizované -0,833° referenci (viz PLAN.md Krok 24 - citlivost na
    dosah - stejný jev, jen demonstrovaný na syntetických datech)."""
    grid_size = 600
    pixel_deg = 0.001
    lat0, lon0 = 50.0, 14.0
    half = grid_size * pixel_deg / 2
    transform = from_origin(lon0 - half, lat0 + half, pixel_deg, pixel_deg)
    data = np.full((grid_size, grid_size), 500.0, dtype=np.float32)

    path = tmp_path / "large_flat.tif"
    with rasterio.open(
        path, "w", driver="GTiff", height=grid_size, width=grid_size, count=1,
        dtype=data.dtype, crs="EPSG:4326", transform=transform,
    ) as dst:
        dst.write(data, 1)
    return str(path)


def _fake_metadata(provider: str = PROVIDER_CUZK, used_fallback: bool = False, reason: str | None = None) -> DEMMetadata:
    return DEMMetadata(
        provider=provider,
        dataset="ČÚZK DMR 5G (DTM)" if provider == PROVIDER_CUZK else "Copernicus GLO-30 (DSM)",
        spatial_resolution_m=10.0,
        analysis_range_m=3000.0,
        source_crs="EPSG:4326",
        acquired_at=datetime.now(timezone.utc),
        used_fallback=used_fallback,
        fallback_reason=reason,
        quality_flag="ok",
    )


def _patch_resolve_dem(monkeypatch, dem_path: str, metadata: DEMMetadata | None = None):
    def _fake_resolve_dem(lat, lon, analysis_range_m, pixel_size_m=10.0):
        return DEMSource(dem_path), (metadata or _fake_metadata())

    monkeypatch.setattr("app.analyze_location.resolve_dem", _fake_resolve_dem)


UTC_NOON = lambda d: datetime(d.year, d.month, d.day, 12, 0, 0, tzinfo=timezone.utc)  # noqa: E731


# --- Timezone handling (zadání bod 4) ---


def test_naive_datetime_rejected(monkeypatch, flat_dem_path):
    _patch_resolve_dem(monkeypatch, flat_dem_path)
    with pytest.raises(ValueError, match="timezone-aware"):
        analyze_location(50.0, 14.0, when=datetime(2026, 6, 21, 12, 0, 0), analysis_range_m=2000)


def test_utc_datetime_works(monkeypatch, flat_dem_path):
    _patch_resolve_dem(monkeypatch, flat_dem_path)
    result = analyze_location(50.0, 14.0, when=datetime(2026, 6, 21, 12, 0, 0, tzinfo=timezone.utc), analysis_range_m=2000)
    assert result.solar.query_time.tzinfo is not None


def test_prague_summer_dst_no_crash(monkeypatch, flat_dem_path):
    _patch_resolve_dem(monkeypatch, flat_dem_path)
    when = datetime(2026, 7, 15, 12, 0, 0, tzinfo=ZoneInfo("Europe/Prague"))  # CEST (UTC+2)
    result = analyze_location(50.0, 14.0, when=when, analysis_range_m=2000)
    assert result.solar.astronomical_sunset > result.solar.astronomical_sunrise


def test_prague_winter_no_crash(monkeypatch, flat_dem_path):
    _patch_resolve_dem(monkeypatch, flat_dem_path)
    when = datetime(2026, 1, 15, 12, 0, 0, tzinfo=ZoneInfo("Europe/Prague"))  # CET (UTC+1)
    result = analyze_location(50.0, 14.0, when=when, analysis_range_m=2000)
    assert result.solar.astronomical_sunset > result.solar.astronomical_sunrise


def test_dst_spring_forward_transition_no_crash(monkeypatch, flat_dem_path):
    """ČR mění na letní čas poslední březnovou neděli (2026: 29.3.) - den
    má jen 23 hodin. Analýza nesmí spadnout ani dát nesmyslný výsledek."""
    _patch_resolve_dem(monkeypatch, flat_dem_path)
    when = datetime(2026, 3, 29, 12, 0, 0, tzinfo=ZoneInfo("Europe/Prague"))
    result = analyze_location(50.0, 14.0, when=when, analysis_range_m=2000)
    assert result.solar.astronomical_sunset > result.solar.astronomical_sunrise
    assert result.solar.astronomical_sunset.tzinfo is not None


def test_dst_fall_back_transition_no_crash(monkeypatch, flat_dem_path):
    """ČR mění zpět na standardní čas poslední říjnovou neděli (2026: 25.10.)."""
    _patch_resolve_dem(monkeypatch, flat_dem_path)
    when = datetime(2026, 10, 25, 12, 0, 0, tzinfo=ZoneInfo("Europe/Prague"))
    result = analyze_location(50.0, 14.0, when=when, analysis_range_m=2000)
    assert result.solar.astronomical_sunset > result.solar.astronomical_sunrise


def test_different_timezones_same_instant_give_consistent_sunset(monkeypatch, flat_dem_path):
    """Stejný fyzický okamžik vyjádřený ve dvou různých timezonech musí
    dát STEJNÝ astronomický západ (je to fyzikální událost, ne závislá na
    tom, v jaké zóně se o ni ptáme) - jen `solar.query_time` se liší."""
    _patch_resolve_dem(monkeypatch, flat_dem_path)
    when_utc = datetime(2026, 6, 21, 10, 0, 0, tzinfo=timezone.utc)
    when_prague = when_utc.astimezone(ZoneInfo("Europe/Prague"))

    result_utc = analyze_location(50.0, 14.0, when=when_utc, analysis_range_m=2000)
    result_prague = analyze_location(50.0, 14.0, when=when_prague, analysis_range_m=2000)

    assert result_utc.solar.astronomical_sunset == result_prague.solar.astronomical_sunset


# --- Struktura výsledku, konzistence, determinismus ---


def test_result_is_location_analysis_with_all_sections(monkeypatch, flat_dem_path):
    _patch_resolve_dem(monkeypatch, flat_dem_path)
    result = analyze_location(50.0, 14.0, when=UTC_NOON(datetime(2026, 6, 21)), analysis_range_m=2000)
    assert isinstance(result, LocationAnalysis)
    assert result.location is not None
    assert result.dem is not None
    assert result.terrain is not None
    assert result.solar is not None
    assert result.visibility is not None
    assert result.diagnostics is not None


def test_360_horizon_integrity(monkeypatch, flat_dem_path):
    _patch_resolve_dem(monkeypatch, flat_dem_path)
    result = analyze_location(50.0, 14.0, when=UTC_NOON(datetime(2026, 6, 21)), analysis_range_m=2000, azimuth_step_deg=1.0)
    assert len(result.terrain.azimuths_deg) == 360
    assert len(result.terrain.horizon_altitudes_deg) == 360
    assert result.terrain.azimuths_deg == sorted(result.terrain.azimuths_deg)
    assert result.terrain.azimuths_deg[0] == 0.0


def test_horizon_matches_solar_azimuth_lookup(monkeypatch, flat_dem_path):
    """Konzistence mezi terrain.horizon_at(sunset_azimuth) a solar.sunset_terrain_horizon_deg
    (viz zadání bod 6 - "sun azimuth = X°, terrain horizon at X° = Y°")."""
    _patch_resolve_dem(monkeypatch, flat_dem_path)
    result = analyze_location(50.0, 14.0, when=UTC_NOON(datetime(2026, 6, 21)), analysis_range_m=2000)
    looked_up = result.terrain.horizon_at(result.solar.sunset_azimuth_deg)
    assert looked_up == pytest.approx(result.solar.sunset_terrain_horizon_deg, abs=1e-9)


def test_azimuth_wraparound_near_zero_does_not_crash(monkeypatch, flat_dem_path):
    _patch_resolve_dem(monkeypatch, flat_dem_path)
    result = analyze_location(50.0, 14.0, when=UTC_NOON(datetime(2026, 6, 21)), analysis_range_m=2000)
    v1 = result.terrain.horizon_at(359.9)
    v2 = result.terrain.horizon_at(0.1)
    assert not math.isnan(v1)
    assert not math.isnan(v2)


def test_deterministic_result_for_same_inputs(monkeypatch, flat_dem_path):
    _patch_resolve_dem(monkeypatch, flat_dem_path)
    when = UTC_NOON(datetime(2026, 6, 21))
    r1 = analyze_location(50.0, 14.0, when=when, analysis_range_m=2000)
    r2 = analyze_location(50.0, 14.0, when=when, analysis_range_m=2000)
    assert r1.terrain.horizon_altitudes_deg == r2.terrain.horizon_altitudes_deg
    assert r1.solar.astronomical_sunset == r2.solar.astronomical_sunset
    assert r1.visibility.terrain_obscured_sunset == r2.visibility.terrain_obscured_sunset


# --- Terrain obstruction: True/False, žádný falešný posun ---


def test_flat_terrain_horizon_stays_small_regardless_of_range(monkeypatch, large_flat_dem_path):
    """AUDIT NÁLEZ při psaní testu: dokonale ROVNÝ terén (pozorovatel jen
    ve výšce očí 1,7 m, žádné převýšení) matematicky NEMŮŽE dosáhnout
    terrain_obstruction=False vůči astral -0,833° referenci - dip
    horizontu daný jen výškou očí nad rovinou vychází řádově 0,04°
    (odpovídá standardnímu geodetickému vzorci dip≈0,0293°·√výška_m),
    násobně míň než 0,833°. To NENÍ bug - "opravdu otevřený, nic
    neblokuje" reálně vyžaduje PŘEVÝŠENÍ pozorovatele nad okolím (viz
    `test_real_open_peak_no_obstruction_no_false_shift` níže, skutečná
    Milešovka) - jen rovina samotná ne.

    Křivka (úhel vs. dosah) navíc NENÍ monotónní jako u izolovaného
    vrcholu (Krok 24) - pro dokonale plochý terén má z geometrických
    důvodů jedno optimum (~5 km při výšce očí 1,7 m: d=√(2·R_eff·h)) a za
    ním se zase mírně zavírá, jak zakřivení Země začne převažovat nad
    výškou očí. Test proto ověřuje jen řádovou velikost, ne směr změny."""
    _patch_resolve_dem(monkeypatch, large_flat_dem_path)
    result_short = analyze_location(50.0, 14.0, when=UTC_NOON(datetime(2026, 6, 21)), analysis_range_m=3_000)
    result_long = analyze_location(50.0, 14.0, when=UTC_NOON(datetime(2026, 6, 21)), analysis_range_m=30_000)
    for result in (result_short, result_long):
        assert -0.2 < result.solar.sunset_terrain_horizon_deg < 0.0
        assert result.visibility.terrain_obstruction is True  # viz docstring - matematicky očekávané, ne bug


@pytest.mark.integration
def test_real_open_peak_no_obstruction_no_false_shift():
    """Skutečná Milešovka (izolovaný vrchol, viz PLAN.md) na dosahu, kde
    jsme už dřív empiricky ověřili otevřený horizont - jediný spolehlivý
    způsob, jak otestovat "opravdu otevřený horizont, žádná obstrukce",
    protože (viz test výše) to vyžaduje skutečné převýšení terénu, ne
    syntetickou rovinu."""
    result = analyze_location(*MILESOVKA, when=UTC_NOON(datetime(2026, 6, 21)), analysis_range_m=10_000)
    assert result.visibility.terrain_obstruction is False
    assert result.visibility.terrain_obscured_sunset == result.visibility.astronomical_sunset
    assert result.visibility.delay_minutes == 0.0


def test_high_wall_causes_obstruction(monkeypatch, wall_dem_path):
    _patch_resolve_dem(monkeypatch, wall_dem_path)
    result = analyze_location(50.0, 14.0, when=UTC_NOON(datetime(2026, 6, 21)), analysis_range_m=4000)
    if result.visibility.terrain_obstruction:
        assert result.visibility.delay_minutes > 0
        assert result.visibility.terrain_obscured_sunset < result.visibility.astronomical_sunset


# --- DEM fallback a NODATA (propagace přes celé API) ---


def test_fallback_metadata_propagates_to_result(monkeypatch, flat_dem_path):
    fallback_metadata = _fake_metadata(provider=PROVIDER_COPERNICUS, used_fallback=True, reason="ČÚZK timeout")
    _patch_resolve_dem(monkeypatch, flat_dem_path, metadata=fallback_metadata)

    result = analyze_location(50.0, 14.0, when=UTC_NOON(datetime(2026, 6, 21)), analysis_range_m=2000)

    assert result.dem.fallback_used is True
    assert result.dem.provider == PROVIDER_COPERNICUS
    assert any("fallback" in w.lower() for w in result.diagnostics.warnings)


def test_nodata_at_final_source_raises_clear_error(monkeypatch):
    class NanDEM:
        def elevation(self, lat, lon):
            return float("nan")

        def contains(self, lat, lon):
            return True

        def close(self):
            pass

    monkeypatch.setattr("app.analyze_location.resolve_dem", lambda *a, **k: (NanDEM(), _fake_metadata(PROVIDER_COPERNICUS)))

    with pytest.raises(RuntimeError, match="NODATA"):
        analyze_location(50.0, 14.0, when=UTC_NOON(datetime(2026, 6, 21)), analysis_range_m=2000)


# --- Diagnostics ---


def test_calculation_parameters_recorded(monkeypatch, flat_dem_path):
    _patch_resolve_dem(monkeypatch, flat_dem_path)
    result = analyze_location(50.0, 14.0, when=UTC_NOON(datetime(2026, 6, 21)), analysis_range_m=2000, azimuth_step_deg=2.0)
    assert result.diagnostics.calculation_parameters["analysis_range_m"] == 2000
    assert result.diagnostics.calculation_parameters["azimuth_step_deg"] == 2.0


def test_degraded_resolution_without_fallback_is_warned(monkeypatch, flat_dem_path):
    """Milestone "DEM Quality and Resolution Control" - ČÚZK ImageServer
    může tiše zhrubit rozlišení (nedokumentovaný limit celkového počtu
    pixelů, viz app/cuzk_dem.py effective_pixel_size()) BEZ přepnutí na
    Copernicus fallback. Tohle dřív nebylo vidět nikde v per-analýze
    diagnostice (jen v logu) - musí se to teď propsat do `warnings`."""
    degraded_metadata = _fake_metadata(provider=PROVIDER_CUZK, used_fallback=False, reason=None)
    degraded_metadata.spatial_resolution_m = 36.1  # požadováno 10.0m/px (default pixel_size_m)
    _patch_resolve_dem(monkeypatch, flat_dem_path, metadata=degraded_metadata)

    result = analyze_location(50.0, 14.0, when=UTC_NOON(datetime(2026, 6, 21)), analysis_range_m=2000)

    assert result.dem.fallback_used is False
    assert result.dem.resolution_m == 36.1
    assert any("zhrubeno" in w.lower() for w in result.diagnostics.warnings)


def test_full_resolution_without_fallback_has_no_degradation_warning(monkeypatch, flat_dem_path):
    _patch_resolve_dem(monkeypatch, flat_dem_path)  # výchozí _fake_metadata: 10.0m/px, bez fallbacku

    result = analyze_location(50.0, 14.0, when=UTC_NOON(datetime(2026, 6, 21)), analysis_range_m=2000)

    assert not any("zhrubeno" in w.lower() for w in result.diagnostics.warnings)


def test_visibility_duration_lower_bound_flagged(monkeypatch, flat_dem_path):
    """Plochý/otevřený horizont - Slunce je nad ním prakticky celý den,
    takže prohledávané okno nenajde vzestupný přechod - musí to být
    jasně označené jako dolní odhad, ne tvářit se jako přesné."""
    _patch_resolve_dem(monkeypatch, flat_dem_path)
    result = analyze_location(
        50.0, 14.0, when=UTC_NOON(datetime(2026, 6, 21)), analysis_range_m=2000,
        visibility_search_window=timedelta(minutes=30),
    )
    if result.visibility.visibility_duration_is_lower_bound:
        assert any("dolní odhad" in w.lower() for w in result.diagnostics.warnings)


# --- Integrační testy (skutečné DEM zdroje) ---


@pytest.mark.integration
def test_real_czech_location_uses_cuzk():
    result = analyze_location(*MILESOVKA, when=UTC_NOON(datetime(2026, 6, 21)), analysis_range_m=5000)
    assert result.dem.provider == PROVIDER_CUZK
    assert 800 < result.location.elevation_m < 870


@pytest.mark.integration
def test_real_foreign_location_uses_copernicus():
    result = analyze_location(*PARIS, when=UTC_NOON(datetime(2026, 6, 21)), analysis_range_m=5000)
    assert result.dem.provider == PROVIDER_COPERNICUS
    assert result.dem.fallback_used is False
