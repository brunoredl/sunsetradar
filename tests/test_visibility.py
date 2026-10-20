"""Testy pro app/visibility.py - Krok 4 (obecná is_sun_visible primitiva,
nezávislá na východu/západu) a Krok 5 (bisekce pro nalezení okamžiku
zakrytí Slunce terénem)."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import numpy as np
import pytest

from app.horizon import HorizonProfile
from app.solar import SunPosition, make_observer
from app.visibility import (
    estimate_sunrise_visibility,
    estimate_sunset_visibility,
    find_horizon_crossing,
    is_sun_visible,
)


def _flat_horizon(value_deg: float = 0.0) -> HorizonProfile:
    azimuths = np.arange(0, 360, 1.0)
    return HorizonProfile(azimuths_deg=azimuths, horizon_altitude_deg=np.full_like(azimuths, value_deg))


def test_is_sun_visible_above_horizon():
    horizon = _flat_horizon(2.0)
    assert is_sun_visible(sun_altitude_deg=10.0, sun_azimuth_deg=270.0, horizon=horizon) is True


def test_is_sun_visible_below_horizon():
    horizon = _flat_horizon(2.0)
    assert is_sun_visible(sun_altitude_deg=1.0, sun_azimuth_deg=270.0, horizon=horizon) is False


def test_is_sun_visible_uses_correct_azimuth_not_global_value():
    # horizont je 10° na severu, ale 0° na jihu - stejná výška Slunce musí
    # vyjít jinak podle TOHO, kterým směrem Slunce zrovna je.
    azimuths = np.array([0.0, 90.0, 180.0, 270.0, 359.0])
    values = np.array([10.0, 0.0, 0.0, 0.0, 10.0])
    horizon = HorizonProfile(azimuths_deg=azimuths, horizon_altitude_deg=values)

    assert is_sun_visible(5.0, 0.0, horizon) is False  # sever: horizont 10°, Slunce jen 5° - zakryté
    assert is_sun_visible(5.0, 180.0, horizon) is True  # jih: horizont 0°, Slunce 5° - viditelné


def test_estimate_sunset_visibility_open_horizon_matches_astronomical():
    observer = make_observer(50.5552, 13.9312, 837.0)
    horizon = _flat_horizon(-5.0)  # hodně otevřený horizont, žádná reálná překážka
    result = estimate_sunset_visibility(observer, date(2026, 9, 22), horizon)
    assert result.terrain_obstructed is False
    assert result.visible_until_time == result.astronomical_sunset_time


def test_estimate_sunset_visibility_high_wall_obstructs_early():
    observer = make_observer(50.5552, 13.9312, 837.0)
    horizon = _flat_horizon(20.0)  # vysoká "zeď" všude kolem
    result = estimate_sunset_visibility(observer, date(2026, 9, 22), horizon, search_window_before=timedelta(hours=6))
    assert result.terrain_obstructed is True
    assert result.visible_until_time < result.astronomical_sunset_time


def test_bisection_crossing_time_matches_manual_linear_scan():
    """Ověří přesnost bisekce (Krok 5) proti hrubému lineárnímu skenu se
    stejnou toleranci - musí se shodnout na pár sekund."""
    from app.solar import sun_position

    observer = make_observer(50.5552, 13.9312, 837.0)
    horizon = _flat_horizon(3.0)
    d = date(2026, 9, 22)

    from app.solar import astronomical_sunset

    sunset_time = astronomical_sunset(observer, d)
    crossing = find_horizon_crossing(
        observer, horizon, sunset_time - timedelta(hours=2), sunset_time + timedelta(minutes=10), direction="descending"
    )
    assert crossing is not None

    # manuální hrubý sken po 5s okolo nalezeného okamžiku pro nezávislé ověření
    t = crossing - timedelta(seconds=30)
    end = crossing + timedelta(seconds=30)
    manual_crossing = None
    prev_above = None
    while t <= end:
        pos = sun_position(observer, t)
        above = pos.altitude_deg > horizon.altitude_at(pos.azimuth_deg)
        if prev_above is True and not above:
            manual_crossing = t
            break
        prev_above = above
        t += timedelta(seconds=1)

    assert manual_crossing is not None
    assert abs((crossing - manual_crossing).total_seconds()) <= 2.0


def test_find_horizon_crossing_returns_none_when_no_crossing_in_window():
    observer = make_observer(50.5552, 13.9312, 837.0)
    horizon = _flat_horizon(-30.0)  # nikdy nezakryje - Slunce nikdy neklesne tak nízko v tomhle okně
    d = date(2026, 9, 22)
    from app.solar import astronomical_sunset

    sunset_time = astronomical_sunset(observer, d)
    crossing = find_horizon_crossing(
        observer, horizon, sunset_time - timedelta(minutes=5), sunset_time + timedelta(minutes=5), direction="descending"
    )
    assert crossing is None


def test_sunrise_visibility_symmetric_to_sunset_for_flat_horizon():
    observer = make_observer(50.5552, 13.9312, 837.0)
    horizon = _flat_horizon(-5.0)
    result = estimate_sunrise_visibility(observer, date(2026, 9, 22), horizon)
    assert result.terrain_obstructed is False
    assert result.visible_from_time == result.astronomical_sunrise_time


def test_sunrise_visibility_wall_delays_sunrise():
    observer = make_observer(50.5552, 13.9312, 837.0)
    horizon = _flat_horizon(20.0)
    result = estimate_sunrise_visibility(observer, date(2026, 9, 22), horizon, search_window_after=timedelta(hours=6))
    assert result.terrain_obstructed is True
    assert result.visible_from_time > result.astronomical_sunrise_time


# --- Fáze 4: occurrence="first" vs. "last" pro nemonotónní profily
# (potřeba pro visibility_duration - "kdy se Slunce naposledy vynořilo,
# než definitivně zapadlo", ne první výskyt) ---


def test_find_horizon_crossing_last_occurrence_finds_later_crossing(monkeypatch):
    """Syntetická (ne reálná) trajektorie výšky Slunce se dvěma sestupnými
    přechody přes plochý horizont (0°) - simuluje dočasné zmizení za
    bližším hrbolem (min ~15) a definitivní západ za hlavním horizontem
    (min ~40). `occurrence='first'` musí najít ten dřívější, `'last'` ten
    pozdější - ověřeno nezávisle na skutečném astronomickém výpočtu
    (sun_position je tu zfalšovaná), aby test byl deterministický a
    přesně kontrolovaný."""
    import app.visibility as visibility_module

    t0 = datetime(2026, 9, 22, 15, 0, 0, tzinfo=timezone.utc)

    def fake_altitude(minutes: float) -> float:
        if minutes < 15:
            return 5.0 - minutes * 0.2  # klesá 5.0 -> 2.0, zůstává nad horizontem
        if minutes < 25:
            return -3.0  # dočasné zakrytí (např. bližší hrbol)
        return 8.0 - minutes * 0.2  # znovu nad horizontem (~3.0 v min 25), pak klesá k definitivnímu západu okolo min 40

    def fake_sun_position(observer, t):
        minutes = (t - t0).total_seconds() / 60
        return SunPosition(time=t, azimuth_deg=270.0, altitude_deg=fake_altitude(minutes))

    monkeypatch.setattr(visibility_module, "sun_position", fake_sun_position)

    observer = make_observer(50.0, 14.0, 0.0)
    horizon = _flat_horizon(0.0)

    first = find_horizon_crossing(
        observer, horizon, t0, t0 + timedelta(minutes=60), direction="descending",
        coarse_step_seconds=60, occurrence="first",
    )
    last = find_horizon_crossing(
        observer, horizon, t0, t0 + timedelta(minutes=60), direction="descending",
        coarse_step_seconds=60, occurrence="last",
    )

    assert first is not None and last is not None
    assert last > first
    assert (t0 + timedelta(minutes=13)) <= first <= (t0 + timedelta(minutes=16))
    assert (t0 + timedelta(minutes=38)) <= last <= (t0 + timedelta(minutes=41))


def test_find_horizon_crossing_first_and_last_identical_when_only_one_crossing():
    observer = make_observer(50.5552, 13.9312, 837.0)
    horizon = _flat_horizon(-5.0)
    from app.solar import astronomical_sunset

    d = date(2026, 9, 22)
    sunset_time = astronomical_sunset(observer, d)

    first = find_horizon_crossing(
        observer, horizon, sunset_time - timedelta(hours=1), sunset_time + timedelta(minutes=20),
        direction="descending", occurrence="first",
    )
    last = find_horizon_crossing(
        observer, horizon, sunset_time - timedelta(hours=1), sunset_time + timedelta(minutes=20),
        direction="descending", occurrence="last",
    )
    assert first == last
