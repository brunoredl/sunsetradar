"""Fáze 4 - jedna veřejná funkce `analyze_location()`, která orchestruje
všechny existující komponenty (DEM strategie, terénní horizont,
astronomie, viditelnost) do jednoho strukturovaného výsledku.

DŮLEŽITÉ: tohle je čistě ORCHESTRACE - žádná výpočetní logika tu není
přepsaná ani duplikovaná. Každá komponenta (`app.dem_strategy`,
`app.horizon`, `app.solar`, `app.visibility`, `app.terrain_attributes`)
zůstává nezávisle použitelná a testovaná sama o sobě.

Terminologie (viz PLAN.md sekce "Fáze 4" pro plné zdůvodnění):
  - `astronomical_sunset`: okamžik, kdy Slunce dosáhne standardní
    astronomické výšky pro západ (viz `app.solar.astronomical_sunset` -
    astral, včetně refrakce a "dip" podle nadmořské výšky pozorovatele).
  - `terrain_obscured_sunset`: okamžik, kdy Slunce přestane být
    geometricky viditelné kvůli terénnímu horizontu v příslušném azimutu.
    ROVNÁ SE `astronomical_sunset`, pokud terén nepřekáží (`terrain_obstruction=False`)
    - žádný falešný posun se nikdy nevytváří.
  - `visibility_duration`: doba od okamžiku, kdy Slunce naposledy vystoupilo
    nad lokální terénní horizont, do `terrain_obscured_sunset`. Pokud tenhle
    "vzestupný" okamžik neleží uvnitř prohledávaného okna (Slunce bylo nad
    horizontem po celou dobu okna), hodnota je jen DOLNÍ ODHAD a
    `visibility_duration_is_lower_bound=True` - nikdy se to nepředstírá
    jako přesné, pokud přesné není (viz diagnostics.warnings)."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from app.dem_strategy import DEFAULT_ANALYSIS_RANGE_M, DEMMetadata, resolve_dem
from app.horizon import DEFAULT_REFRACTION_COEFFICIENT, HorizonProfile, compute_horizon_profile
from app.solar import make_observer, sun_position
from app.terrain_attributes import compute_slope_aspect
from app.visibility import estimate_sunrise_visibility, estimate_sunset_visibility, find_horizon_crossing

DEFAULT_AZIMUTH_STEP_DEG = 1.0
DEFAULT_HORIZON_STEP_M = 90.0
DEFAULT_OBSERVER_EYE_HEIGHT_M = 1.7
DEFAULT_PIXEL_SIZE_M = 10.0
DEFAULT_VISIBILITY_SEARCH_WINDOW = timedelta(hours=3)


@dataclass
class LocationInfo:
    latitude: float
    longitude: float
    elevation_m: float  # nadmořská výška TERÉNU (bez výšky očí pozorovatele)


@dataclass
class DemInfo:
    provider: str
    dataset: str
    resolution_m: float
    analysis_range_m: float
    source_crs: str
    acquired_at: datetime | None
    fallback_used: bool
    fallback_reason: str | None
    quality_flag: str

    @classmethod
    def from_metadata(cls, metadata: DEMMetadata) -> "DemInfo":
        return cls(
            provider=metadata.provider,
            dataset=metadata.dataset,
            resolution_m=metadata.spatial_resolution_m,
            analysis_range_m=metadata.analysis_range_m,
            source_crs=metadata.source_crs,
            acquired_at=metadata.acquired_at,
            fallback_used=metadata.used_fallback,
            fallback_reason=metadata.fallback_reason,
            quality_flag=metadata.quality_flag,
        )


@dataclass
class TerrainInfo:
    """`azimuths_deg`/`horizon_altitudes_deg` jsou VŽDY stejně dlouhé
    seznamy (azimuth -> výškový úhel terénního horizontu) - viz zadání
    bod 5, "neztrácej tato data pouze převodem na jedno číslo". NaN
    hodnoty znamenají "DEM tam nepokrývalo dost daleko" (viz app.horizon)."""

    azimuths_deg: list[float]
    horizon_altitudes_deg: list[float]
    minimum_horizon_deg: float
    maximum_horizon_deg: float
    slope_deg: float | None
    aspect_deg: float | None
    _profile: HorizonProfile = field(repr=False, compare=False)

    def horizon_at(self, azimuth_deg: float) -> float:
        """Interpolovaný terénní horizont pro libovolný azimut (i mimo
        vzorkovanou mřížku) - deleguje na `HorizonProfile.altitude_at`,
        včetně wraparound přes šev 359°/0°."""
        return self._profile.altitude_at(azimuth_deg)


@dataclass
class SolarInfo:
    query_time: datetime  # `when` echované zpátky (timezone-aware)
    current_azimuth_deg: float
    current_altitude_deg: float
    astronomical_sunset: datetime
    sunset_azimuth_deg: float
    sunset_terrain_horizon_deg: float  # terén.horizon_at(sunset_azimuth_deg) - viz zadání bod 6
    astronomical_sunrise: datetime | None
    sunrise_azimuth_deg: float | None
    sunrise_terrain_horizon_deg: float | None


@dataclass
class VisibilityInfo:
    terrain_obstruction: bool
    astronomical_sunset: datetime
    terrain_obscured_sunset: datetime  # == astronomical_sunset, pokud terrain_obstruction je False (žádný falešný posun)
    delay_minutes: float  # 0.0, pokud terrain_obstruction je False
    visibility_duration: timedelta | None
    visibility_duration_is_lower_bound: bool

    sunrise_terrain_obstruction: bool | None
    terrain_revealed_sunrise: datetime | None
    sunrise_delay_minutes: float | None


@dataclass
class Diagnostics:
    warnings: list[str] = field(default_factory=list)
    calculation_parameters: dict[str, Any] = field(default_factory=dict)


@dataclass
class LocationAnalysis:
    location: LocationInfo
    dem: DemInfo
    terrain: TerrainInfo
    solar: SolarInfo
    visibility: VisibilityInfo
    diagnostics: Diagnostics


@dataclass
class StaticTerrain:
    """Milestone "Persistent Terrain Cache" - přesně ta část výsledku
    `analyze_location()`, která závisí JEN na poloze (lat/lon) a
    analytických parametrech (analysis_range_m, azimuth_step_deg, ...),
    NIKDY na `when` (datum/čas). Horizont vrcholu je stejný dnes i za
    měsíc - proto se dá tahle část bezpečně persistentně cachovat a
    znovupoužít pro libovolné datum (viz `app/terrain_cache.py`).

    Audit před refaktorem (viz PLAN.md): `analyze_location()` už dřív
    interně počítala přesně tohle na začátku, než se `when` použilo
    poprvé (`sunset_date = when.date()`) - `compute_static_terrain()`
    níže je jen POJMENOVANÉ VYTAŽENÍ existujícího bloku kódu, ŽÁDNÁ
    změna algoritmu/matematiky (zadání bod 3 - "SAME INPUT + SAME
    ENGINE = SAME RESULT")."""

    location: LocationInfo
    dem: DemInfo
    terrain: TerrainInfo
    warnings: list[str]


def compute_static_terrain(
    lat: float,
    lon: float,
    analysis_range_m: float = DEFAULT_ANALYSIS_RANGE_M,
    azimuth_step_deg: float = DEFAULT_AZIMUTH_STEP_DEG,
    horizon_step_m: float = DEFAULT_HORIZON_STEP_M,
    observer_eye_height_m: float = DEFAULT_OBSERVER_EYE_HEIGHT_M,
    pixel_size_m: float = DEFAULT_PIXEL_SIZE_M,
    refraction_coefficient: float = DEFAULT_REFRACTION_COEFFICIENT,
) -> StaticTerrain:
    """DRAHÁ část analýzy (DEM stažení/čtení + 360° horizon scan) -
    přesně tohle je to, co se persistentně cachuje. Nezávisí na datu."""
    warnings: list[str] = []

    dem, dem_metadata = resolve_dem(lat, lon, analysis_range_m, pixel_size_m)
    try:
        if dem_metadata.used_fallback:
            warnings.append(f"Použit fallback DEM zdroj ({dem_metadata.provider}): {dem_metadata.fallback_reason}")
        elif dem_metadata.spatial_resolution_m > pixel_size_m * 1.001:
            # Milestone "DEM Quality and Resolution Control" - ČÚZK
            # ImageServer může tiše zhrubit rozlišení kvůli nedokumentovanému
            # limitu celkového počtu pixelů (viz app/cuzk_dem.py,
            # effective_pixel_size()), BEZ přepnutí na Copernicus fallback.
            # Dřív se tohle propsalo jen do logu (app/dem_strategy.py), ne
            # do per-analýzy diagnostiky - teď je to vidět i tady, a tedy
            # i v `static_warnings_json` persistentní cache (viz
            # app/terrain_cache.py save_ready()).
            warnings.append(
                f"Efektivní rozlišení DEM zhrubeno na {dem_metadata.spatial_resolution_m:.1f}m/px "
                f"(požadováno {pixel_size_m:.1f}m/px) - {dem_metadata.provider} server limit pro tuto oblast, "
                "bez změny zdroje (žádný fallback)."
            )

        observer_elevation_m = dem.elevation(lat, lon)
        if math.isnan(observer_elevation_m):
            # Poslední zdroj (Copernicus) selhal na NODATA - žádný další
            # fallback neexistuje (viz PLAN.md Krok 26, 7.3) - jasná chyba
            # je lepší než tichý výsledek postavený na NaN.
            raise RuntimeError(
                f"Nepodařilo se získat nadmořskou výšku pro ({lat}, {lon}) ani z {dem_metadata.provider} "
                "(NODATA) - žádný další DEM zdroj není k dispozici."
            )

        # Terénní horizont počítáme JEDNOU (celý kruh) a znovupoužijeme pro
        # sunset i sunrise viditelnost - viz AUDIT NÁLEZ (Fáze 4, výkon).
        profile = compute_horizon_profile(
            dem,
            lat,
            lon,
            observer_eye_height_m,
            azimuth_start_deg=0.0,
            azimuth_end_deg=360.0 - azimuth_step_deg,
            azimuth_step_deg=azimuth_step_deg,
            max_distance_m=analysis_range_m,
            step_m=horizon_step_m,
            observer_elevation_m=observer_elevation_m,
            refraction_coefficient=refraction_coefficient,
        )

        n_nan = sum(1 for v in profile.horizon_altitude_deg if math.isnan(v))
        if n_nan:
            warnings.append(
                f"{n_nan} z {len(profile.horizon_altitude_deg)} azimutů nemá terénní data "
                f"(DEM nepokrývá celý požadovaný dosah {analysis_range_m/1000:.0f} km ve všech směrech)."
            )

        valid_altitudes = [v for v in profile.horizon_altitude_deg if not math.isnan(v)]
        min_horizon = min(valid_altitudes) if valid_altitudes else float("nan")
        max_horizon = max(valid_altitudes) if valid_altitudes else float("nan")

        slope_deg: float | None
        aspect_deg: float | None
        try:
            slope_aspect = compute_slope_aspect(dem, lat, lon)
            slope_deg, aspect_deg = slope_aspect.slope_deg, slope_aspect.aspect_deg
        except Exception as exc:  # sklon/expozice je informační doplněk (viz Fáze 3, Krok 9) - nesmí shodit celou analýzu
            slope_deg, aspect_deg = None, None
            warnings.append(f"Sklon/expozice se nepodařilo spočítat: {exc}")

        terrain = TerrainInfo(
            azimuths_deg=[float(a) for a in profile.azimuths_deg],
            horizon_altitudes_deg=[float(v) for v in profile.horizon_altitude_deg],
            minimum_horizon_deg=min_horizon,
            maximum_horizon_deg=max_horizon,
            slope_deg=slope_deg,
            aspect_deg=aspect_deg,
            _profile=profile,
        )

        location = LocationInfo(latitude=lat, longitude=lon, elevation_m=observer_elevation_m)
        dem_info = DemInfo.from_metadata(dem_metadata)

        return StaticTerrain(location=location, dem=dem_info, terrain=terrain, warnings=warnings)
    finally:
        dem.close()


def compute_dynamic_sunset(
    static: StaticTerrain,
    when: datetime,
    observer_eye_height_m: float = DEFAULT_OBSERVER_EYE_HEIGHT_M,
    visibility_search_window: timedelta = DEFAULT_VISIBILITY_SEARCH_WINDOW,
    include_sunrise: bool = True,
) -> tuple[SolarInfo, VisibilityInfo, list[str]]:
    """LEVNÁ část analýzy (astronomie + porovnání se SPOČÍTANÝM horizontem,
    žádný DEM přístup) - závisí na `when`, MUSÍ se počítat pro každé
    datum znovu, i když `static` pochází z cache. `observer_eye_height_m`
    se předává znovu (ne z `static`), protože ovlivňuje jen výšku
    pozorovatele nad UŽ ZNÁMÝM terénem, ne samotný horizon scan."""
    if when.tzinfo is None:
        raise ValueError(
            "compute_dynamic_sunset() vyžaduje timezone-aware `when` (např. datetime s tzinfo=timezone.utc, "
            "nebo zoneinfo.ZoneInfo('Europe/Prague')) - žádná implicitní lokální časová zóna stroje."
        )

    warnings: list[str] = []
    profile = static.terrain._profile
    observer_elevation_m = static.location.elevation_m
    observer = make_observer(static.location.latitude, static.location.longitude, observer_elevation_m + observer_eye_height_m)
    current_pos = sun_position(observer, when)

    sunset_date = when.date()
    sunset_result = estimate_sunset_visibility(observer, sunset_date, profile, search_window_before=visibility_search_window)

    sunrise_azimuth_deg: float | None = None
    sunrise_terrain_horizon_deg: float | None = None
    astronomical_sunrise: datetime | None = None
    sunrise_terrain_obstruction: bool | None = None
    terrain_revealed_sunrise: datetime | None = None
    sunrise_delay_minutes: float | None = None

    if include_sunrise:
        sunrise_result = estimate_sunrise_visibility(observer, sunset_date, profile, search_window_after=visibility_search_window)
        astronomical_sunrise = sunrise_result.astronomical_sunrise_time
        sunrise_azimuth_deg = sunrise_result.astronomical_sunrise_azimuth_deg
        sunrise_terrain_horizon_deg = sunrise_result.terrain_horizon_at_sunrise_azimuth_deg
        sunrise_terrain_obstruction = sunrise_result.terrain_obstructed
        terrain_revealed_sunrise = sunrise_result.visible_from_time
        sunrise_delay_minutes = (
            (sunrise_result.visible_from_time - sunrise_result.astronomical_sunrise_time).total_seconds() / 60.0
            if sunrise_result.terrain_obstructed
            else 0.0
        )

    solar = SolarInfo(
        query_time=when,
        current_azimuth_deg=current_pos.azimuth_deg,
        current_altitude_deg=current_pos.altitude_deg,
        astronomical_sunset=sunset_result.astronomical_sunset_time,
        sunset_azimuth_deg=sunset_result.astronomical_sunset_azimuth_deg,
        sunset_terrain_horizon_deg=sunset_result.terrain_horizon_at_sunset_azimuth_deg,
        astronomical_sunrise=astronomical_sunrise,
        sunrise_azimuth_deg=sunrise_azimuth_deg,
        sunrise_terrain_horizon_deg=sunrise_terrain_horizon_deg,
    )

    delay_minutes = 0.0
    if sunset_result.terrain_obstructed:
        delay_minutes = (sunset_result.astronomical_sunset_time - sunset_result.visible_until_time).total_seconds() / 60.0

    visibility_duration, duration_is_lower_bound = _compute_visibility_duration(
        observer, profile, sunset_result.visible_until_time, visibility_search_window
    )
    if duration_is_lower_bound:
        warnings.append(
            "visibility_duration je jen DOLNÍ ODHAD - Slunce bylo nad terénním horizontem po celou "
            f"prohledávanou dobu ({visibility_search_window}), skutečný začátek viditelnosti mohl být dřív."
        )

    visibility = VisibilityInfo(
        terrain_obstruction=sunset_result.terrain_obstructed,
        astronomical_sunset=sunset_result.astronomical_sunset_time,
        terrain_obscured_sunset=sunset_result.visible_until_time,
        delay_minutes=delay_minutes,
        visibility_duration=visibility_duration,
        visibility_duration_is_lower_bound=duration_is_lower_bound,
        sunrise_terrain_obstruction=sunrise_terrain_obstruction,
        terrain_revealed_sunrise=terrain_revealed_sunrise,
        sunrise_delay_minutes=sunrise_delay_minutes,
    )
    return solar, visibility, warnings


def analyze_location(
    lat: float,
    lon: float,
    when: datetime,
    analysis_range_m: float = DEFAULT_ANALYSIS_RANGE_M,
    azimuth_step_deg: float = DEFAULT_AZIMUTH_STEP_DEG,
    horizon_step_m: float = DEFAULT_HORIZON_STEP_M,
    observer_eye_height_m: float = DEFAULT_OBSERVER_EYE_HEIGHT_M,
    pixel_size_m: float = DEFAULT_PIXEL_SIZE_M,
    refraction_coefficient: float = DEFAULT_REFRACTION_COEFFICIENT,
    visibility_search_window: timedelta = DEFAULT_VISIBILITY_SEARCH_WINDOW,
    include_sunrise: bool = True,
) -> LocationAnalysis:
    """Kompletní fyzikální analýza západu (a volitelně východu) Slunce pro
    daný bod a čas - jedna veřejná funkce, čistě orchestrující existující
    komponenty (viz docstring modulu).

    `when` MUSÍ být timezone-aware (viz zadání bod 4) - žádná implicitní
    lokální časová zóna stroje. Určuje jak KALENDÁŘNÍ DEN (pro výpočet
    západu/východu), tak okamžik pro `solar.current_*` pole - v obojím
    případě v časové zóně, kterou `when` sám nese (funkce ji nijak
    nepřevádí, jen respektuje).

    `analysis_range_m` výchozí `DEFAULT_ANALYSIS_RANGE_M` (50 km, viz
    `app.dem_strategy` a PLAN.md Krok 24) - citlivostní analýza ukázala,
    že přesnost se zlepšuje i mezi 30 a 50 km.

    Milestone "Persistent Terrain Cache": tahle funkce je teď čistě
    `compute_static_terrain()` + `compute_dynamic_sunset()` složené
    dohromady - STEJNÝ výpočet jako předtím (žádná změna algoritmu),
    jen rozdělený na cacheovatelnou a datum-závislou část (viz
    `app/terrain_cache.py`, který volá obě části zvlášť)."""
    if when.tzinfo is None:
        raise ValueError(
            "analyze_location() vyžaduje timezone-aware `when` (např. datetime s tzinfo=timezone.utc, "
            "nebo zoneinfo.ZoneInfo('Europe/Prague')) - žádná implicitní lokální časová zóna stroje."
        )

    static = compute_static_terrain(
        lat, lon, analysis_range_m, azimuth_step_deg, horizon_step_m, observer_eye_height_m, pixel_size_m, refraction_coefficient
    )
    solar, visibility, dynamic_warnings = compute_dynamic_sunset(
        static, when, observer_eye_height_m, visibility_search_window, include_sunrise
    )

    diagnostics = Diagnostics(
        warnings=static.warnings + dynamic_warnings,
        calculation_parameters={
            "analysis_range_m": analysis_range_m,
            "azimuth_step_deg": azimuth_step_deg,
            "horizon_step_m": horizon_step_m,
            "observer_eye_height_m": observer_eye_height_m,
            "pixel_size_m": pixel_size_m,
            "refraction_coefficient": refraction_coefficient,
            "visibility_search_window_seconds": visibility_search_window.total_seconds(),
        },
    )

    return LocationAnalysis(
        location=static.location, dem=static.dem, terrain=static.terrain, solar=solar, visibility=visibility, diagnostics=diagnostics
    )


def _compute_visibility_duration(
    observer, profile: HorizonProfile, end_time: datetime, search_window: timedelta
) -> tuple[timedelta | None, bool]:
    """Doba od posledního vzestupného přechodu (Slunce nad lokální terénní
    horizont) do `end_time` (= terrain_obscured_sunset, nebo astronomical_sunset
    pokud terén nepřekáží). Hledá se POSLEDNÍ takový přechod (`occurrence="last"`)
    v okně `[end_time - search_window, end_time)` - u nemonotónního horizontu
    (Slunce se může krátce schovat za bližší hrbol a znovu vynořit) by první
    nalezený přechod nemusel být ten, který skutečně předchází finálnímu
    zakrytí (viz app.visibility)."""
    search_start = end_time - search_window
    last_ascending = find_horizon_crossing(
        observer, profile, search_start, end_time - timedelta(seconds=1), direction="ascending", occurrence="last"
    )

    if last_ascending is None:
        # Slunce bylo nad horizontem po celou prohledávanou dobu - nevíme,
        # kdy "opravdu" vystoupilo, jen že to bylo dřív než search_start.
        return end_time - search_start, True

    return end_time - last_ascending, False
