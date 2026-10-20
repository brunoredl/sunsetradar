"""Testy pro app/dem_strategy.py (Krok 7) - výběr providera podle
lokality, fallback ČÚZK -> Copernicus za všech scénářů ze zadání (7.9):
mimo ČR, chyba/timeout, NODATA, podezřelá kvalita. Selhání ČÚZK se
simuluje monkeypatchem (rychlé, deterministické, bez sítě) - skutečné
síťové chování je pokryté zvlášť v tests/test_cuzk_dem.py."""

from __future__ import annotations

import math

import pytest
import requests

from app.dem_strategy import (
    PROVIDER_COPERNICUS,
    PROVIDER_CUZK,
    resolve_dem,
)
from app.cuzk_dem import OutsideCoverageError


class FakeDEMSource:
    """Minimální DEMProvider-kompatibilní dvojník pro testy fallbacku -
    nechceme kvůli testu chyby skutečně stahovat žádná data."""

    def __init__(self, elevation_value: float):
        self._elevation_value = elevation_value
        self.closed = False

    def elevation(self, lat: float, lon: float) -> float:
        return self._elevation_value

    def contains(self, lat: float, lon: float) -> bool:
        return True

    def close(self) -> None:
        self.closed = True


PARIS = (48.8566, 2.3522)  # mimo ČR
MILESOVKA = (50.5552, 13.9312)  # v ČR


def test_outside_cz_uses_copernicus_directly_without_trying_cuzk(monkeypatch):
    def _fail_if_called(*args, **kwargs):
        raise AssertionError("ČÚZK se nemělo vůbec zkoušet mimo ČR")

    monkeypatch.setattr("app.dem_strategy.open_cuzk_dmr5g_dem", _fail_if_called)
    monkeypatch.setattr("app.dem_strategy.open_copernicus_dem", lambda lat, lon, r: FakeDEMSource(100.0))

    dem, metadata = resolve_dem(*PARIS, analysis_range_m=10_000)

    assert metadata.provider == PROVIDER_COPERNICUS
    assert metadata.used_fallback is False
    assert metadata.fallback_reason is None


def test_cz_point_success_uses_cuzk_with_correct_metadata(monkeypatch):
    fake = FakeDEMSource(831.9)
    monkeypatch.setattr("app.dem_strategy.open_cuzk_dmr5g_dem", lambda *a, **k: fake)

    dem, metadata = resolve_dem(*MILESOVKA, analysis_range_m=10_000)

    assert dem is fake
    assert metadata.provider == PROVIDER_CUZK
    assert "DTM" in metadata.dataset
    assert metadata.used_fallback is False
    assert metadata.quality_flag == "ok"
    assert metadata.acquired_at is not None
    assert metadata.analysis_range_m == 10_000


def test_fallback_when_cuzk_outside_coverage(monkeypatch):
    def _raise_outside(*a, **k):
        raise OutsideCoverageError("mimo pokrytí")

    monkeypatch.setattr("app.dem_strategy.open_cuzk_dmr5g_dem", _raise_outside)
    monkeypatch.setattr("app.dem_strategy.open_copernicus_dem", lambda lat, lon, r: FakeDEMSource(100.0))

    dem, metadata = resolve_dem(*MILESOVKA, analysis_range_m=10_000)

    assert metadata.provider == PROVIDER_COPERNICUS
    assert metadata.used_fallback is True
    assert "mimo pokrytí" in metadata.fallback_reason


def test_fallback_on_timeout(monkeypatch):
    def _raise_timeout(*a, **k):
        raise requests.exceptions.Timeout("timed out")

    monkeypatch.setattr("app.dem_strategy.open_cuzk_dmr5g_dem", _raise_timeout)
    monkeypatch.setattr("app.dem_strategy.open_copernicus_dem", lambda lat, lon, r: FakeDEMSource(100.0))

    dem, metadata = resolve_dem(*MILESOVKA, analysis_range_m=10_000)

    assert metadata.provider == PROVIDER_COPERNICUS
    assert metadata.used_fallback is True
    assert "timed out" in metadata.fallback_reason


def test_fallback_on_server_error(monkeypatch):
    def _raise_http_error(*a, **k):
        raise requests.exceptions.HTTPError("500 Server Error")

    monkeypatch.setattr("app.dem_strategy.open_cuzk_dmr5g_dem", _raise_http_error)
    monkeypatch.setattr("app.dem_strategy.open_copernicus_dem", lambda lat, lon, r: FakeDEMSource(100.0))

    dem, metadata = resolve_dem(*MILESOVKA, analysis_range_m=10_000)

    assert metadata.provider == PROVIDER_COPERNICUS
    assert metadata.used_fallback is True


def test_fallback_on_malformed_response(monkeypatch):
    """`app.cuzk_dem` hlásí "server vrátil JSON místo obrázku" jako RuntimeError."""

    def _raise_runtime(*a, **k):
        raise RuntimeError("ČÚZK ImageServer vrátil chybu místo obrázku: {...}")

    monkeypatch.setattr("app.dem_strategy.open_cuzk_dmr5g_dem", _raise_runtime)
    monkeypatch.setattr("app.dem_strategy.open_copernicus_dem", lambda lat, lon, r: FakeDEMSource(100.0))

    dem, metadata = resolve_dem(*MILESOVKA, analysis_range_m=10_000)

    assert metadata.provider == PROVIDER_COPERNICUS
    assert metadata.used_fallback is True


def test_fallback_on_nodata_at_observer_point(monkeypatch):
    fake = FakeDEMSource(float("nan"))
    monkeypatch.setattr("app.dem_strategy.open_cuzk_dmr5g_dem", lambda *a, **k: fake)
    monkeypatch.setattr("app.dem_strategy.open_copernicus_dem", lambda lat, lon, r: FakeDEMSource(100.0))

    dem, metadata = resolve_dem(*MILESOVKA, analysis_range_m=10_000)

    assert metadata.provider == PROVIDER_COPERNICUS
    assert metadata.used_fallback is True
    assert "NODATA" in metadata.fallback_reason
    assert fake.closed is True  # nepoužitelný zdroj se má zavřít, ne nechat viset


def test_fallback_on_suspicious_quality(monkeypatch):
    # zjevně nesmyslná nadmořská výška pro ČR (pod úrovní Labe, nebo nad Sněžkou)
    fake = FakeDEMSource(-500.0)
    monkeypatch.setattr("app.dem_strategy.open_cuzk_dmr5g_dem", lambda *a, **k: fake)
    monkeypatch.setattr("app.dem_strategy.open_copernicus_dem", lambda lat, lon, r: FakeDEMSource(100.0))

    dem, metadata = resolve_dem(*MILESOVKA, analysis_range_m=10_000)

    assert metadata.provider == PROVIDER_COPERNICUS
    assert metadata.used_fallback is True
    assert "podezřelá" in metadata.fallback_reason
    assert fake.closed is True


def test_cz_point_with_large_range_reports_degraded_resolution_in_metadata(monkeypatch):
    """Milestone "DEM Quality and Resolution Control" - reprodukuje
    skutečně zjištěný případ (precompute běh, --limit 5): na velký
    `analysis_range_m` (odpovídá DEFAULT_ANALYSIS_RANGE_M=50000) ČÚZK
    ImageServer limit (viz app/cuzk_dem.py MAX_TOTAL_PIXELS) vynutí
    zhrubení rozlišení. `open_cuzk_dmr5g_dem` je mockované (žádná síť),
    ale `bbox_for_area`/`effective_pixel_size` jsou REÁLNÉ, čisté funkce
    - ověřujeme, že `_try_cuzk()` jejich výsledek skutečně převezme do
    `DEMMetadata.spatial_resolution_m`, ne nějakou nominální hodnotu."""
    fake = FakeDEMSource(831.9)
    monkeypatch.setattr("app.dem_strategy.open_cuzk_dmr5g_dem", lambda *a, **k: fake)

    dem, metadata = resolve_dem(*MILESOVKA, analysis_range_m=50_000, pixel_size_m=10.0)

    assert metadata.provider == PROVIDER_CUZK
    assert metadata.used_fallback is False
    assert metadata.spatial_resolution_m > 10.0  # skutečně zhrubeno, ne tichá nominální hodnota
    from app.cuzk_dem import bbox_for_area, effective_pixel_size

    bbox = bbox_for_area(*MILESOVKA, 50_000)
    assert metadata.spatial_resolution_m == effective_pixel_size(bbox, 10.0)


def test_describe_includes_fallback_and_quality_markers(monkeypatch):
    fake = FakeDEMSource(float("nan"))
    monkeypatch.setattr("app.dem_strategy.open_cuzk_dmr5g_dem", lambda *a, **k: fake)
    monkeypatch.setattr("app.dem_strategy.open_copernicus_dem", lambda lat, lon, r: FakeDEMSource(100.0))

    _, metadata = resolve_dem(*MILESOVKA, analysis_range_m=10_000)
    description = metadata.describe()

    assert "Copernicus" in description
    assert "fallback" in description


@pytest.mark.integration
def test_real_resolve_dem_in_cz_uses_cuzk():
    """Živý test proti skutečnému ČÚZK serveru - potvrzuje, že šťastná
    cesta (bez fallbacku) opravdu funguje s reálnými daty, ne jen s fake objekty."""
    dem, metadata = resolve_dem(*MILESOVKA, analysis_range_m=3_000)
    try:
        assert metadata.provider == PROVIDER_CUZK
        assert metadata.used_fallback is False
        elevation = dem.elevation(*MILESOVKA)
        assert 800 < elevation < 870
    finally:
        dem.close()
