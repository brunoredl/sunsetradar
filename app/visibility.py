"""Spojuje polohu Slunce (app/solar.py) s terénním horizontem
(app/horizon.py) a řeší otázku "je/bude Slunce vidět, nebo je/bude
zakryté terénem". Architektura je záměrně obecná (Krok 4) - `is_sun_visible`
je čistá funkce použitelná pro cokoliv (východ, západ, kontrola
konkrétního azimutu pro fotografii, hiking planner...), ne natvrdo
navázaná na "západ Slunce" jako use case.

`estimate_sunset_visibility`/`estimate_sunrise_visibility` jsou tenké
obálky nad obecným `find_horizon_crossing` (Krok 5): nejdřív hrubé
skenování (najde INTERVAL, kde dochází k přechodu), pak bisekce uvnitř
intervalu (zpřesnění na požadovanou toleranci) - viz PLAN.md bod 5 pro
zdůvodnění, proč ne čistě lineární sken (pomalý) ani čistě bisekce od
začátku (potřebuje spolehlivý bracket, který hrubý sken zajistí i pro
nemonotónní horizontové profily - např. Slunce může nakrátko zmizet za
bližším hrbolem a zase se objevit, než definitivně zapadne za hlavní
hřeben)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from astral import Observer

from app.horizon import HorizonProfile
from app.solar import astronomical_sunset, sun_position

try:
    from astral.sun import sunrise as _astral_sunrise
except ImportError:  # pragma: no cover
    _astral_sunrise = None


def is_sun_visible(sun_altitude_deg: float, sun_azimuth_deg: float, horizon: HorizonProfile) -> bool:
    """Obecná primitiva: je Slunce v tomhle okamžiku nad terénním
    horizontem (tedy viditelné), nebo pod ním (zakryté)? Nezávislá na
    tom, jestli jde o východ, západ, nebo jen kontrolu "vidím teď Slunce
    z týhle vyhlídky ano/ne"."""
    return sun_altitude_deg > horizon.altitude_at(sun_azimuth_deg)


@dataclass
class SunsetVisibilityResult:
    observer_lat: float
    observer_lon: float
    astronomical_sunset_time: datetime
    astronomical_sunset_azimuth_deg: float
    astronomical_sunset_sun_altitude_deg: float
    terrain_horizon_at_sunset_azimuth_deg: float
    terrain_obstructed: bool
    visible_until_time: datetime  # = "terrain_obscured_time", pokud terrain_obstructed


def find_horizon_crossing(
    observer: Observer,
    horizon_profile: HorizonProfile,
    t_start: datetime,
    t_end: datetime,
    direction: Literal["descending", "ascending"],
    coarse_step_seconds: int = 60,
    tolerance_seconds: float = 1.0,
    occurrence: Literal["first", "last"] = "first",
) -> datetime | None:
    """Najde okamžik, kdy Slunce protne terénní horizont, mezi `t_start` a
    `t_end`. `direction="descending"` hledá přechod viditelné->zakryté
    (západ), `"ascending"` zakryté->viditelné (východ). `occurrence`
    (Fáze 4, potřeba pro `visibility_duration`): `"first"` (výchozí,
    stejné chování jako předtím) vrátí PRVNÍ nalezený přechod ve směru
    skenování, `"last"` prohledá celé okno a vrátí POSLEDNÍ - potřebné
    např. pro dohledání "kdy se Slunce naposledy vynořilo nad terén, než
    definitivně zapadlo", pokud horizont není monotónní (víc 'zubů').

    Postup: (1) hrubý lineární sken po `coarse_step_seconds` najde
    interval(y), kde dochází k požadovanému přechodu (robustní i vůči
    nemonotónnímu profilu), (2) bisekce uvnitř vybraného intervalu
    zpřesní čas na `tolerance_seconds`. Vrací None, pokud se v okně žádný
    takový přechod nenajde."""

    def f(t: datetime) -> float:
        pos = sun_position(observer, t)
        return pos.altitude_deg - horizon_profile.altitude_at(pos.azimuth_deg)

    def is_crossing(prev_f: float, curr_f: float) -> bool:
        if direction == "descending":
            return prev_f > 0 and curr_f <= 0
        return prev_f <= 0 and curr_f > 0

    t_prev = t_start
    f_prev = f(t_prev)
    t = t_start + timedelta(seconds=coarse_step_seconds)

    bracket: tuple[datetime, datetime] | None = None
    while t <= t_end:
        f_curr = f(t)
        if is_crossing(f_prev, f_curr):
            bracket = (t_prev, t)
            if occurrence == "first":
                break
        t_prev, f_prev = t, f_curr
        t += timedelta(seconds=coarse_step_seconds)

    if bracket is None:
        return None

    lo, hi = bracket
    f_lo = f(lo)
    while (hi - lo).total_seconds() > tolerance_seconds:
        mid = lo + (hi - lo) / 2
        f_mid = f(mid)
        if is_crossing(f_lo, f_mid):
            hi = mid  # přechod je v [lo, mid] - zúžit shora
        else:
            lo, f_lo = mid, f_mid  # přechod je v [mid, hi] - zúžit zdola
    return lo + (hi - lo) / 2


def estimate_sunset_visibility(
    observer: Observer,
    date,
    horizon_profile: HorizonProfile,
    search_window_before: timedelta = timedelta(minutes=120),
    search_window_after: timedelta = timedelta(minutes=20),
    coarse_step_seconds: int = 60,
    tolerance_seconds: float = 1.0,
) -> SunsetVisibilityResult:
    sunset_time = astronomical_sunset(observer, date)
    sunset_pos = sun_position(observer, sunset_time)
    horizon_at_sunset_az = horizon_profile.altitude_at(sunset_pos.azimuth_deg)

    crossing_time = find_horizon_crossing(
        observer,
        horizon_profile,
        sunset_time - search_window_before,
        sunset_time + search_window_after,
        direction="descending",
        coarse_step_seconds=coarse_step_seconds,
        tolerance_seconds=tolerance_seconds,
    )

    # Terén "obstruuje", pokud Slunce zmizí za ním DŘÍV, než by zapadlo
    # astronomicky - jinak (crossing chybí, nebo nastane až po/na
    # astronomickém západu) je horizont dost otevřený a platí standardní čas.
    terrain_obstructed = crossing_time is not None and crossing_time < sunset_time - timedelta(seconds=coarse_step_seconds)
    visible_until = crossing_time if terrain_obstructed else sunset_time

    return SunsetVisibilityResult(
        observer_lat=observer.latitude,
        observer_lon=observer.longitude,
        astronomical_sunset_time=sunset_time,
        astronomical_sunset_azimuth_deg=sunset_pos.azimuth_deg,
        astronomical_sunset_sun_altitude_deg=sunset_pos.altitude_deg,
        terrain_horizon_at_sunset_azimuth_deg=horizon_at_sunset_az,
        terrain_obstructed=terrain_obstructed,
        visible_until_time=visible_until,
    )


@dataclass
class SunriseVisibilityResult:
    observer_lat: float
    observer_lon: float
    astronomical_sunrise_time: datetime
    astronomical_sunrise_azimuth_deg: float
    terrain_horizon_at_sunrise_azimuth_deg: float
    terrain_obstructed: bool
    visible_from_time: datetime


def estimate_sunrise_visibility(
    observer: Observer,
    date,
    horizon_profile: HorizonProfile,
    search_window_before: timedelta = timedelta(minutes=20),
    search_window_after: timedelta = timedelta(minutes=120),
    coarse_step_seconds: int = 60,
    tolerance_seconds: float = 1.0,
) -> SunriseVisibilityResult:
    """Stejný princip jako `estimate_sunset_visibility`, jen naopak -
    dokazuje, že engine (Krok 4) není natvrdo navázaný na západ Slunce."""
    if _astral_sunrise is None:  # pragma: no cover
        raise RuntimeError("astral.sun.sunrise není dostupný v této verzi astral")

    sunrise_time = _astral_sunrise(observer, date=date)
    sunrise_pos = sun_position(observer, sunrise_time)
    horizon_at_sunrise_az = horizon_profile.altitude_at(sunrise_pos.azimuth_deg)

    crossing_time = find_horizon_crossing(
        observer,
        horizon_profile,
        sunrise_time - search_window_before,
        sunrise_time + search_window_after,
        direction="ascending",
        coarse_step_seconds=coarse_step_seconds,
        tolerance_seconds=tolerance_seconds,
    )

    terrain_obstructed = crossing_time is not None and crossing_time > sunrise_time + timedelta(seconds=coarse_step_seconds)
    visible_from = crossing_time if terrain_obstructed else sunrise_time

    return SunriseVisibilityResult(
        observer_lat=observer.latitude,
        observer_lon=observer.longitude,
        astronomical_sunrise_time=sunrise_time,
        astronomical_sunrise_azimuth_deg=sunrise_pos.azimuth_deg,
        terrain_horizon_at_sunrise_azimuth_deg=horizon_at_sunrise_az,
        terrain_obstructed=terrain_obstructed,
        visible_from_time=visible_from,
    )
