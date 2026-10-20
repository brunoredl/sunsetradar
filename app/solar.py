"""Poloha Slunce (azimut, výška nad horizontem) pro dané GPS + datum/čas.

Používá `astral` - lehká čistě-pythonová knihovna bez těžkých numerických
závislostí, dost přesná (řádově setiny stupně) na porovnání s terénním
horizontem, jehož nejistota je stejně větší (daná rozlišením DEM). Pro
budoucí potřebu přesnějšího modelu (atmosférická refrakce, irradiance)
existuje `pvlib` se Solar Position Algorithm (SPA) - viz PLAN.md.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from astral import Observer
from astral.sun import azimuth as _azimuth
from astral.sun import elevation as _elevation
from astral.sun import sunset as _astral_sunset


@dataclass
class SunPosition:
    time: datetime
    azimuth_deg: float  # 0-360, měřeno od severu po směru hodinových ručiček
    altitude_deg: float  # výška nad astronomickým horizontem (0° = horizont, záporné = pod)


def make_observer(lat: float, lon: float, elevation_m: float = 0.0) -> Observer:
    return Observer(latitude=lat, longitude=lon, elevation=elevation_m)


def sun_position(observer: Observer, when: datetime) -> SunPosition:
    """Azimut + výška Slunce pro daný okamžik. `when` musí být timezone-aware
    (astral interně počítá v UTC, ale vyžaduje explicitní tzinfo, aby se
    předešlo tichým chybám s lokálním časem)."""
    if when.tzinfo is None:
        raise ValueError("`when` musí být timezone-aware (např. datetime s tzinfo=timezone.utc)")
    return SunPosition(
        time=when,
        azimuth_deg=_azimuth(observer, when),
        altitude_deg=_elevation(observer, when),
    )


def astronomical_sunset(observer: Observer, date) -> datetime:
    """Čas astronomického západu (střed slunečního kotouče protíná
    matematický horizont, tj. výška ≈ -0.833° kvůli standardní refrakci a
    poloměru Slunce - astral to počítá interně stejně jako všechny
    'oficiální' časy západu)."""
    return _astral_sunset(observer, date=date)


def sun_track_around_sunset(
    observer: Observer, date, window_before: timedelta, window_after: timedelta, step_seconds: int = 30
) -> list[SunPosition]:
    """Vrátí polohu Slunce v pravidelných krocích kolem astronomického
    západu - použije se k nalezení okamžiku, kdy výška Slunce klesne pod
    terénní horizont v daném azimutu (viz app/horizon.py)."""
    center = astronomical_sunset(observer, date)
    start = center - window_before
    end = center + window_after

    positions = []
    t = start
    while t <= end:
        positions.append(sun_position(observer, t))
        t += timedelta(seconds=step_seconds)
    return positions
