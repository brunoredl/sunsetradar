"""Testy pro app/dem_provider.py - ověřují, že existující DEMSource
strukturálně splňuje DEMProvider protokol beze změny, a že terrain engine
(app.horizon) přijímá cokoliv, co protokol splňuje - ne natvrdo jen
DEMSource."""

from __future__ import annotations

from app.dem import DEMSource
from app.dem_provider import DEMProvider


def test_demsource_satisfies_demprovider_protocol(flat_dem_path):
    with DEMSource(flat_dem_path) as dem:
        assert isinstance(dem, DEMProvider)


def test_fake_provider_also_satisfies_protocol():
    class FakeProvider:
        def elevation(self, lat: float, lon: float) -> float:
            return 123.0

        def contains(self, lat: float, lon: float) -> bool:
            return True

    assert isinstance(FakeProvider(), DEMProvider)


def test_horizon_engine_works_with_any_demprovider_not_just_demsource(flat_dem_path):
    """Terrain engine (app.horizon) nesmí být natvrdo napsaný na
    app.dem.DEMSource - ověříme na jednoduché fake implementaci
    DEMProvider protokolu se stejným rozhraním."""
    from app.horizon import terrain_angle

    class FlatFakeProvider:
        def elevation(self, lat: float, lon: float) -> float:
            return 500.0

        def contains(self, lat: float, lon: float) -> bool:
            return True

    fake = FlatFakeProvider()
    angle = terrain_angle(fake, 50.0, 14.0, 1.7, azimuth_deg=270.0, max_distance_m=3000, step_m=100)
    assert -3.0 < angle < 1.0  # stejné chování jako u skutečného plochého DEM (viz test_horizon.py)
