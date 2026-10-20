"""Geodetické výpočty nad WGS84 elipsoidem - bod ve vzdálenosti a azimutu
od pozorovatele. Používá `pyproj.Geod` (přesný geodetický výpočet, ne
plochou aproximaci) - na vzdálenostech desítek km už plochá aproximace
zavádí znatelnou chybu."""

from __future__ import annotations

from pyproj import Geod

_GEOD = Geod(ellps="WGS84")


def destination_point(lat: float, lon: float, azimuth_deg: float, distance_m: float) -> tuple[float, float]:
    """Vrátí (lat, lon) bodu ve vzdálenosti `distance_m` metrů od (lat, lon)
    ve směru `azimuth_deg` (0° = sever, po směru hodinových ručiček)."""
    lon2, lat2, _back_azimuth = _GEOD.fwd(lon, lat, azimuth_deg, distance_m)
    return lat2, lon2


def geodesic_distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    _fwd_az, _back_az, distance = _GEOD.inv(lon1, lat1, lon2, lat2)
    return distance
