"""Sanity testy pro app/solar.py - žádná DEM data, jen astronomie."""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from app.solar import astronomical_sunset, make_observer, sun_position


def test_naive_datetime_rejected():
    observer = make_observer(50.0, 14.0, 0)
    with pytest.raises(ValueError):
        sun_position(observer, datetime(2026, 6, 21, 12, 0, 0))  # bez tzinfo


def test_solar_noon_azimuth_faces_south_on_northern_hemisphere():
    observer = make_observer(50.0, 14.0, 0)
    noon_utc = datetime(2026, 6, 21, 11, 0, 0, tzinfo=timezone.utc)  # ~ místní pravé poledne
    pos = sun_position(observer, noon_utc)
    # V létě na severní polokouli je Slunce v pravé poledne na jihu (~180°)
    assert 150 < pos.azimuth_deg < 210


def test_altitude_decreases_towards_sunset():
    observer = make_observer(50.0, 14.0, 0)
    sunset_time = astronomical_sunset(observer, date(2026, 6, 21))
    earlier = sunset_time.replace(hour=max(sunset_time.hour - 2, 0))
    pos_earlier = sun_position(observer, earlier)
    pos_at_sunset = sun_position(observer, sunset_time)
    assert pos_at_sunset.altitude_deg < pos_earlier.altitude_deg


def test_astronomical_sunset_altitude_is_near_zero():
    observer = make_observer(50.0, 14.0, 0)
    sunset_time = astronomical_sunset(observer, date(2026, 6, 21))
    pos = sun_position(observer, sunset_time)
    # Standardní refrakce + poloměr Slunce ~ -0.83° u pozorovatele na
    # úrovni terénu (elevation=0) - tolerance kvůli algoritmu v astral.
    assert -1.5 < pos.altitude_deg < 0.5


def test_higher_observer_elevation_delays_geometric_sunset():
    # Z vyššího bodu je "geometrický" horizont níž (dip due to elevation),
    # takže astronomický západ nastane o něco POZDĚJI než na úrovni moře.
    low_observer = make_observer(50.0, 14.0, 0)
    high_observer = make_observer(50.0, 14.0, 2000)
    d = date(2026, 6, 21)
    assert astronomical_sunset(high_observer, d) > astronomical_sunset(low_observer, d)
