"""Sanity testy pro app/geo.py - geodetické výpočty nad WGS84."""

from __future__ import annotations

from app.geo import destination_point, geodesic_distance_m


def test_moving_north_increases_latitude():
    lat, lon = destination_point(50.0, 14.0, 0.0, 10_000)  # 10 km na sever
    assert lat > 50.0
    assert abs(lon - 14.0) < 0.01  # délka by se neměla prakticky změnit


def test_moving_east_increases_longitude():
    lat, lon = destination_point(50.0, 14.0, 90.0, 10_000)  # 10 km na východ
    assert lon > 14.0
    assert abs(lat - 50.0) < 0.01


def test_distance_round_trip_matches_requested_distance():
    lat2, lon2 = destination_point(50.0, 14.0, 271.9, 12_345)
    d = geodesic_distance_m(50.0, 14.0, lat2, lon2)
    assert abs(d - 12_345) < 1.0  # metr přesnosti stačí


def test_one_degree_latitude_is_about_111_km():
    lat, lon = destination_point(50.0, 14.0, 0.0, 111_000)
    assert 0.9 < (lat - 50.0) < 1.1
