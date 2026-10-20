"""Testy pro app/horizon.py - syntetický DEM (viz conftest.py), aby testy
nezávisely na stažených reálných datech."""

from __future__ import annotations

import math

import numpy as np

from app.dem import DEMSource
from app.geo import geodesic_distance_m
from app.horizon import HorizonProfile, compute_horizon_profile, earth_curvature_drop_m, terrain_angle

OBSERVER_LAT = 50.0
OBSERVER_LON = 14.0
EYE_HEIGHT_M = 1.7


def test_curvature_drop_zero_at_zero_distance():
    assert earth_curvature_drop_m(0.0) == 0.0


def test_curvature_drop_increases_with_distance():
    d1 = earth_curvature_drop_m(1_000)
    d2 = earth_curvature_drop_m(10_000)
    d3 = earth_curvature_drop_m(30_000)
    assert d1 < d2 < d3


def test_curvature_drop_matches_closed_form():
    # drop = distance^2 / (2 * R_eff), R_eff = R / (1 - k)
    distance = 20_000.0
    k = 0.13
    r_eff = 6_371_000.0 / (1 - k)
    expected = distance**2 / (2 * r_eff)
    assert abs(earth_curvature_drop_m(distance, k) - expected) < 1e-6


def test_flat_terrain_gives_small_angle_not_spurious(flat_dem_path):
    """Bez jakékoliv skutečné překážky by terrain_angle NEMĚL vrátit nic
    dramatického - jen malou hodnotu blízko 0° (mírně záporná/kladná podle
    poměru výšky očí a zakřivení Země na daném dosahu)."""
    with DEMSource(flat_dem_path) as dem:
        angle = terrain_angle(dem, OBSERVER_LAT, OBSERVER_LON, EYE_HEIGHT_M, azimuth_deg=270.0, max_distance_m=3000, step_m=50)
    assert -3.0 < angle < 1.0


def test_wall_produces_expected_angle(wall_dem_path):
    """Umělá 'zeď' (300 m nad okolím) o pár desítek pixelů na západ -
    terrain_angle musí najít úhel odpovídající geometrii zdi (výška/
    vzdálenost), ne nic jiného."""
    with DEMSource(wall_dem_path) as dem:
        # Najdi skutečnou vzdálenost ke zdi (v metrech), abychom nemuseli
        # spoléhat na přesný převod stupňů na metry v syntetickém gridu.
        wall_lat, wall_lon = OBSERVER_LAT, OBSERVER_LON - 30 * 0.0002  # stejný posun jako ve fixture
        wall_distance = geodesic_distance_m(OBSERVER_LAT, OBSERVER_LON, wall_lat, wall_lon)

        angle = terrain_angle(
            dem, OBSERVER_LAT, OBSERVER_LON, EYE_HEIGHT_M, azimuth_deg=270.0, max_distance_m=5000, step_m=5
        )

    expected_angle = math.degrees(math.atan2(300.0 - EYE_HEIGHT_M, wall_distance))
    assert abs(angle - expected_angle) < 1.0  # tolerance kvůli bilineární interpolaci a diskrétnímu kroku


def test_azimuth_without_obstruction_differs_from_azimuth_with_wall(wall_dem_path):
    with DEMSource(wall_dem_path) as dem:
        angle_towards_wall = terrain_angle(dem, OBSERVER_LAT, OBSERVER_LON, EYE_HEIGHT_M, 270.0, 5000, 5)
        angle_away_from_wall = terrain_angle(dem, OBSERVER_LAT, OBSERVER_LON, EYE_HEIGHT_M, 90.0, 5000, 5)

    assert angle_towards_wall > angle_away_from_wall + 5  # zeď musí být jasně vidět jen v jednom směru


# --- Krok 1 (audit): regresní testy pro NaN sentinel místo -90.0 ---


def test_terrain_angle_returns_nan_when_no_data_reachable(flat_dem_path):
    """Pozorovatel těsně u okraje DEM, krok delší než zbývající prostor -
    ANI JEDEN vzorek podél paprsku není dosažitelný. Dřív se v týhle
    situaci vracelo -90.0 (nerozlišitelné od "opravdu jsem prošel terén a
    je otevřený") - teď musí přijít jasné NaN."""
    with DEMSource(flat_dem_path) as dem:
        b = dem.bounds
        # pozorovatel na severním okraji, směr přímo na sever (pryč z DEM)
        angle = terrain_angle(dem, b.top - 0.00001, OBSERVER_LON, EYE_HEIGHT_M, azimuth_deg=0.0, max_distance_m=5000, step_m=50)
    assert math.isnan(angle)


def test_terrain_angle_skips_single_nodata_sample_without_crashing(nodata_dem_path):
    """Uprostřed paprsku je pás NODATA (viz fixture) - výsledek nesmí
    spadnout, ani se nodata nesmí započítat jako výška 0 (to by vyrobilo
    falešný hluboký zářez v horizontu, přesně to riziko, co má
    _bilinear_sample teď hlídat)."""
    with DEMSource(nodata_dem_path) as dem:
        angle = terrain_angle(dem, OBSERVER_LAT, OBSERVER_LON, EYE_HEIGHT_M, azimuth_deg=270.0, max_distance_m=3000, step_m=20)
    assert not math.isnan(angle)
    assert -3.0 < angle < 1.0  # stejný rozsah jako u čistě plochého terénu (viz výše)


# --- Krok 2: wraparound interpolace přes šev 359°/0° u plného 360° profilu ---


def test_full_circle_profile_interpolates_across_seam():
    azimuths = np.arange(0, 360, 1.0)
    values = azimuths / 36.0  # lineárně roste 0 -> ~10 přes celý kruh
    profile = HorizonProfile(azimuths_deg=azimuths, horizon_altitude_deg=values)

    assert profile.is_full_circle is True
    # 359.5° leží přesně mezi vzorkem na 359° (~9.97) a "obalujícím" vzorkem
    # na 0°/360° (0.0) - průměr obou, NE ořezané na hodnotu z 359°.
    midpoint = profile.altitude_at(359.5)
    assert 4.5 < midpoint < 5.5


def test_partial_sector_profile_does_not_wrap():
    azimuths = np.arange(240, 301, 1.0)
    values = np.zeros_like(azimuths)
    profile = HorizonProfile(azimuths_deg=azimuths, horizon_altitude_deg=values)
    assert profile.is_full_circle is False


# --- Fáze 4 (analyze_location): observer_elevation_m volitelný parametr ---


def test_explicit_observer_elevation_matches_internally_computed(wall_dem_path):
    """Předaná nadmořská výška pozorovatele musí dát STEJNÝ výsledek jako
    když si ji terrain_angle spočítá samo - jde jen o to nepočítat ji
    znovu 360x při 360° sweepu, ne o změnu chování."""
    with DEMSource(wall_dem_path) as dem:
        computed = dem.elevation(OBSERVER_LAT, OBSERVER_LON)
        angle_implicit = terrain_angle(dem, OBSERVER_LAT, OBSERVER_LON, EYE_HEIGHT_M, 270.0, 5000, 5)
        angle_explicit = terrain_angle(
            dem, OBSERVER_LAT, OBSERVER_LON, EYE_HEIGHT_M, 270.0, 5000, 5, observer_elevation_m=computed
        )
    assert angle_implicit == angle_explicit


def test_compute_horizon_profile_only_reads_observer_elevation_once(wall_dem_path, monkeypatch):
    call_count = {"n": 0}
    with DEMSource(wall_dem_path) as dem:
        original_elevation = dem.elevation

        def counting_elevation(lat, lon):
            if lat == OBSERVER_LAT and lon == OBSERVER_LON:
                call_count["n"] += 1
            return original_elevation(lat, lon)

        monkeypatch.setattr(dem, "elevation", counting_elevation)
        compute_horizon_profile(dem, OBSERVER_LAT, OBSERVER_LON, EYE_HEIGHT_M, 0, 350, 10.0, 3000, 200)

    # 36 azimutů (0,10,...350) by dřív znamenalo 36 volání na přesně tenhle
    # bod - teď musí být jen 1 (uvnitř compute_horizon_profile, předané dál).
    assert call_count["n"] == 1


def test_compute_horizon_profile_passes_through_refraction_coefficient(flat_dem_path):
    """AUDIT NÁLEZ (Fáze 4): compute_horizon_profile dřív parametr
    refraction_coefficient vůbec nepřijímala, takže by ho volající (např.
    analyze_location) tiše ignoroval bez ohledu na to, co nastavil."""
    with DEMSource(flat_dem_path) as dem:
        profile_default = compute_horizon_profile(dem, OBSERVER_LAT, OBSERVER_LON, EYE_HEIGHT_M, 260, 280, 5.0, 20000, 200)
        profile_high_k = compute_horizon_profile(
            dem, OBSERVER_LAT, OBSERVER_LON, EYE_HEIGHT_M, 260, 280, 5.0, 20000, 200, refraction_coefficient=0.9
        )
    # výrazně jiný refrakční koeficient musí dát MĚŘITELNĚ jiný výsledek na dlouhém dosahu
    assert not np.allclose(profile_default.horizon_altitude_deg, profile_high_k.horizon_altitude_deg)
