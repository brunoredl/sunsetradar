"""Terénní horizont - výškový úhel, pod kterým terén zakrývá oblohu,
v daném azimutu z pozorovacího bodu. Princip a kompromisy viz PLAN.md
(body 4, 6, 7).

Algoritmus (na azimut): jdi od pozorovatele ve zvoleném azimutu s krokem
`step_m`, do vzdálenosti `max_distance_m`, v každém bodě spočítej úhel
mezi pozorovatelem a terénem (s korekcí na zakřivení Země/refrakci) a
zapamatuj si maximum - to je "terénní horizont" v tomhle směru (nejvyšší
překážka mezi pozorovatelem a obzorem)."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from app.dem_provider import DEMProvider
from app.geo import destination_point

EARTH_RADIUS_M = 6_371_000.0
# Typický koeficient standardní atmosférické refrakce (k≈0.13-0.17 se běžně
# používá pro geodetické/optické výpočty na dálku) - snižuje efektivní
# zakřivení Země o refrakci světla v atmosféře. Viz PLAN.md bod 7.
DEFAULT_REFRACTION_COEFFICIENT = 0.13


@dataclass
class HorizonProfile:
    azimuths_deg: np.ndarray
    horizon_altitude_deg: np.ndarray  # výškový úhel terénního horizontu (°) pro každý azimut

    @property
    def is_full_circle(self) -> bool:
        """True, pokud profil pokrývá celých 360° (Krok 2) - pak
        `altitude_at` musí interpolovat přes šev 359°/0°, ne ho ořezávat."""
        if len(self.azimuths_deg) < 2:
            return False
        span = (self.azimuths_deg[-1] - self.azimuths_deg[0]) + (self.azimuths_deg[1] - self.azimuths_deg[0])
        return bool(span >= 359.9)

    def altitude_at(self, azimuth_deg: float) -> float:
        """Lineárně interpolovaný terénní horizont pro libovolný azimut
        (použije se při porovnání s pohybující se pozicí Slunce, jejíž
        azimut se mezi vzorky posouvá).

        AUDIT NÁLEZ (Krok 2): u plného 360° profilu `np.interp` neumí
        wraparound sám od sebe - dotaz na azimut 359.7° by se bez opravy
        interpoloval jen k poslednímu vzorku (~359°) a ořízl, místo aby
        správně počítal i se vzorkem na 0°/360° (druhá strana švu). Řešeno
        přidáním jednoho "virtuálního" bodu na `azimuth[0] + 360`."""
        az = azimuth_deg % 360.0
        if self.is_full_circle:
            azimuths = np.append(self.azimuths_deg, self.azimuths_deg[0] + 360.0)
            values = np.append(self.horizon_altitude_deg, self.horizon_altitude_deg[0])
            return float(np.interp(az, azimuths, values))
        return float(np.interp(az, self.azimuths_deg, self.horizon_altitude_deg))


def earth_curvature_drop_m(distance_m: float, refraction_coefficient: float = DEFAULT_REFRACTION_COEFFICIENT) -> float:
    """O kolik metrů se vzdálený bod terénu 'propadá' pod tečnou rovinu
    pozorovatele kvůli zakřivení Země (zmenšené o refrakci). Bez týhle
    korekce by se vzdálený terén ve stejné nadmořské výšce jako
    pozorovatel jevil výš, než je - horizont by se systematicky
    NADHODNOTIL (terén by vypadal, že blokuje víc, než reálně blokuje)."""
    effective_radius = EARTH_RADIUS_M / (1 - refraction_coefficient)
    return (distance_m**2) / (2 * effective_radius)


def terrain_angle(
    dem: DEMProvider,
    observer_lat: float,
    observer_lon: float,
    observer_eye_height_m: float,
    azimuth_deg: float,
    max_distance_m: float,
    step_m: float,
    refraction_coefficient: float = DEFAULT_REFRACTION_COEFFICIENT,
    observer_elevation_m: float | None = None,
) -> float:
    """Terénní horizont (°) pro JEDEN azimut.

    Vrací `NaN`, pokud se podél paprsku nepodařilo získat ANI JEDEN platný
    vzorek (pozorovatel je mimo DEM, stojí na NODATA bodě, nebo DEM
    pokrývá jen kousek prvního kroku) - AUDIT NÁLEZ (Krok 1): dřív se v
    téhle situaci vracelo -90.0, což je nerozlišitelné od "opravdu jsme
    prošli terén a je úplně otevřený" (reálný, platný výsledek). NaN dává
    volajícímu jasně najevo "tohle nevím", -90.0 by tvářilo jako
    důvěryhodné číslo.

    `observer_elevation_m`: nadmořská výška TERÉNU (ne včetně výšky očí -
    ta se přičítá až tady stejně jako předtím) na pozici pozorovatele,
    pokud ji volající (typicky `compute_horizon_profile`) už zná. Ušetří
    opakovaný DEM sampling stejného bodu (AUDIT NÁLEZ, Fáze 4: bez
    tohohle parametru se stejný bod čte znovu PŘI KAŽDÉM volání - u 360°
    sweepu 360× stejná hodnota). `None` (výchozí) zachovává přesně
    původní chování - žádný existující volající se nemusí měnit."""
    if observer_elevation_m is not None:
        observer_elevation_raw = observer_elevation_m
    else:
        observer_elevation_raw = dem.elevation(observer_lat, observer_lon)

    if np.isnan(observer_elevation_raw):
        return float("nan")
    observer_elevation = observer_elevation_raw + observer_eye_height_m

    max_angle = float("-inf")
    distance = step_m
    while distance <= max_distance_m:
        lat, lon = destination_point(observer_lat, observer_lon, azimuth_deg, distance)
        if not dem.contains(lat, lon):
            break  # došli jsme na okraj dostupných dat - dál nevidíme

        terrain_elevation = dem.elevation(lat, lon)
        if not np.isnan(terrain_elevation):
            drop = earth_curvature_drop_m(distance, refraction_coefficient)
            height_diff = (terrain_elevation - drop) - observer_elevation
            angle_deg = float(np.degrees(np.arctan2(height_diff, distance)))
            if angle_deg > max_angle:
                max_angle = angle_deg
        # NODATA vzorek uprostřed paprsku (vzácné, ale možné u okrajů
        # dlaždic/vodních ploch) se přeskočí, ne započítá jako 0 - jedno
        # chybějící měření uprostřed dlouhého paprsku by nemělo shodit
        # celý výsledek.

        distance += step_m

    return max_angle if max_angle != float("-inf") else float("nan")


def compute_horizon_profile(
    dem: DEMProvider,
    observer_lat: float,
    observer_lon: float,
    observer_eye_height_m: float,
    azimuth_start_deg: float,
    azimuth_end_deg: float,
    azimuth_step_deg: float,
    max_distance_m: float,
    step_m: float,
    observer_elevation_m: float | None = None,
    refraction_coefficient: float = DEFAULT_REFRACTION_COEFFICIENT,
) -> HorizonProfile:
    """`observer_elevation_m`: viz `terrain_angle` - pokud není zadáno,
    spočítá se tady JEDNOU (ne uvnitř každého z N volání `terrain_angle`
    pro N azimutů, jak tomu bylo předtím - viz AUDIT NÁLEZ, Fáze 4).

    AUDIT NÁLEZ (Fáze 4): dřív se `refraction_coefficient` dalo změnit
    jen přímým voláním `terrain_angle` po jednom azimutu - `compute_horizon_profile`
    parametr vůbec nepřijímala ani nepředávala dál, takže by ho jakýkoliv
    volající kód (např. `analyze_location`) tiše IGNOROVAL, i kdyby ho
    explicitně nastavil. Teď se správně předává do každého volání."""
    if observer_elevation_m is None:
        observer_elevation_m = dem.elevation(observer_lat, observer_lon)

    azimuths = np.arange(azimuth_start_deg, azimuth_end_deg + azimuth_step_deg / 2, azimuth_step_deg)
    horizons = np.array(
        [
            terrain_angle(
                dem, observer_lat, observer_lon, observer_eye_height_m, az, max_distance_m, step_m,
                refraction_coefficient=refraction_coefficient,
                observer_elevation_m=observer_elevation_m,
            )
            for az in azimuths
        ]
    )
    return HorizonProfile(azimuths_deg=azimuths, horizon_altitude_deg=horizons)
