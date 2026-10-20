"""Krok 9: sklon (slope) a expozice (aspect) pozorovacího bodu.

DŮLEŽITÉ (viz zadání): tohle je čistě INFORMAČNÍ doplněk, připravený pro
BUDOUCÍ scoring - NEPOUŽÍVÁ SE nikde ve `visibility.py`/`horizon.py`.
Hlavním faktorem viditelnosti západu zůstává skutečný terénní horizont
(app/horizon.py), ne sklon/expozice bodu, kde pozorovatel stojí -
východní svah automaticky neznamená špatný západ (může mít otevřený
výhled přes údolí navzdory tomu, že samotný svah, na kterém stojíte,
"hledí" na východ)."""

from __future__ import annotations

import math
from dataclasses import dataclass

from app.dem import DEMSource
from app.geo import destination_point


@dataclass
class SlopeAspect:
    slope_deg: float  # 0° = vodorovno, 90° = kolmá stěna
    aspect_deg: float  # kompasový směr SPÁDNICE (kam svah "hledí"/kudy voda stéká) - 0°=sever


def compute_slope_aspect(dem: DEMSource, lat: float, lon: float, sample_distance_m: float = 30.0) -> SlopeAspect:
    """Centrální diference ve 4 směrech (sever/jih/východ/západ) kolem
    bodu - jednodušší než plné Hornovo 3x3 okno (ArcGIS/QGIS standard),
    ale stejný princip a pro jeden bod (ne celý rastr najednou) dostatečně
    přesné; `sample_distance_m` má odpovídat zhruba rozlišení DEM (příliš
    malé by zesilovalo šum jednotlivých pixelů, příliš velké by slope
    "vyhladilo" přes okolní terén)."""
    north_lat, north_lon = destination_point(lat, lon, 0.0, sample_distance_m)
    south_lat, south_lon = destination_point(lat, lon, 180.0, sample_distance_m)
    east_lat, east_lon = destination_point(lat, lon, 90.0, sample_distance_m)
    west_lat, west_lon = destination_point(lat, lon, 270.0, sample_distance_m)

    elev_n = dem.elevation(north_lat, north_lon)
    elev_s = dem.elevation(south_lat, south_lon)
    elev_e = dem.elevation(east_lat, east_lon)
    elev_w = dem.elevation(west_lat, west_lon)

    dz_dy = (elev_n - elev_s) / (2 * sample_distance_m)  # m výšky na m vzdálenosti směrem na sever
    dz_dx = (elev_e - elev_w) / (2 * sample_distance_m)  # m výšky na m vzdálenosti směrem na východ

    slope_deg = math.degrees(math.atan(math.hypot(dz_dx, dz_dy)))

    # Aspekt = kompasový směr SESTUPU (spádnice), ne gradientu (ten míří
    # do kopce) - proto záporné znaménko u obou složek.
    downhill_east = -dz_dx
    downhill_north = -dz_dy
    aspect_deg = math.degrees(math.atan2(downhill_east, downhill_north)) % 360.0

    return SlopeAspect(slope_deg=slope_deg, aspect_deg=aspect_deg)
