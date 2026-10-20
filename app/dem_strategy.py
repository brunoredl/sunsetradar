"""Krok 7 (DEM kvalita a infrastruktura): rozhodnutí o strategii DEM
zdrojů - automatický výběr podle lokality + deterministický fallback +
metadata o použitém zdroji. Viz PLAN.md sekce "DEM Strategy" pro plné
zdůvodnění (7.1-7.6).

Stručně: **v ČR se zkusí ČÚZK DMR 5G (přesnější, skutečné DTM), s
automatickým fallbackem na Copernicus GLO-30 při jakémkoliv problému**
(timeout, chyba serveru, NODATA, podezřelá kvalita, mimo pokrytí). Mimo
ČR se rovnou použije Copernicus (jediná možnost). Žádný krok nevyžaduje
API klíč ani zásah uživatele - výběr je čistě podle GPS souřadnice."""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import datetime, timezone

import requests

from app.cuzk_dem import OutsideCoverageError, effective_pixel_size, is_roughly_in_cz, bbox_for_area
from app.dem import DEMSource
from app.dem_provider import open_copernicus_dem, open_cuzk_dmr5g_dem

logger = logging.getLogger(__name__)

PROVIDER_CUZK = "cuzk_dmr5g"
PROVIDER_COPERNICUS = "copernicus_glo30"

# Krok 24 (citlivostní analýza): rozdíl mezi 30 km a 50 km je pořád ~0,9°
# u otevřených míst - žádný jasný "plateau" nebyl nalezen. Zadání pro
# tenhle krok explicitně říká "nechci optimalizovat výkon za každou cenu,
# důležitější je fyzikálně smysluplný výsledek" - proto výchozí dosah
# 50 km, ne 30 km. Cena je jen delší STAŽENÍ při prvním dotazu do nové
# oblasti (viz Krok 25) - samotný výpočet horizontu zůstává rychlý
# (< 1s i na 50 km, viz Krok 8/15).
DEFAULT_ANALYSIS_RANGE_M = 50_000.0

DEFAULT_PIXEL_SIZE_M = 10.0

# Hrubý sanity-check rozsah nadmořské výšky v ČR (Labe u Hřenska ~115 m,
# Sněžka 1603 m + rezerva) - hodnota mimo tenhle rozsah je "podezřelá
# kvalita" (viz 7.3), spustí fallback na Copernicus místo tichého použití.
CZ_PLAUSIBLE_ELEVATION_RANGE = (90.0, 1650.0)


@dataclass
class DEMMetadata:
    """Metadata o DEM zdroji použitém pro konkrétní analýzu - viz 7.4.
    Cíl: aby UI/report mohl zobrazit např. 'ČÚZK DMR 5G · 10 m · ČR' nebo
    'Copernicus GLO-30 · 30 m · fallback'."""

    provider: str
    dataset: str  # lidsky čitelný název VČETNĚ typu modelu (DTM/DSM)
    spatial_resolution_m: float
    analysis_range_m: float
    source_crs: str
    acquired_at: datetime | None
    used_fallback: bool
    fallback_reason: str | None
    quality_flag: str  # "ok" | "suspicious"

    def describe(self) -> str:
        parts = [self.dataset, f"{self.spatial_resolution_m:.0f} m"]
        if self.used_fallback:
            parts.append("fallback")
        if self.quality_flag != "ok":
            parts.append(self.quality_flag)
        return " · ".join(parts)


def resolve_dem(
    lat: float,
    lon: float,
    analysis_range_m: float = DEFAULT_ANALYSIS_RANGE_M,
    pixel_size_m: float = DEFAULT_PIXEL_SIZE_M,
) -> tuple[DEMSource, DEMMetadata]:
    """Hlavní vstupní bod Kroku 7 - vrátí (DEMSource, DEMMetadata) podle
    deterministického rozhodovacího stromu (viz 7.2-7.3):

        v ČR:
            zkus ČÚZK DMR 5G
                úspěch + rozumná kvalita -> použij ČÚZK
                cokoliv jiné (timeout/chyba/NODATA/podezřelá kvalita) -> fallback na Copernicus
        mimo ČR:
            rovnou Copernicus (ČÚZK by stejně selhalo na OutsideCoverageError)

    Pokud selže i Copernicus, výjimka se NEPOTLAČUJE - žádný třetí zdroj
    neexistuje, takže skrývat chybu by bylo horší než ji nahlásit volajícímu.
    """
    if is_roughly_in_cz(lat, lon):
        dem, metadata = _try_cuzk(lat, lon, analysis_range_m, pixel_size_m)
        if dem is not None:
            return dem, metadata

        logger.warning("ČÚZK DMR 5G selhalo (%s) - fallback na Copernicus GLO-30.", metadata.fallback_reason)
        copernicus_dem = open_copernicus_dem(lat, lon, analysis_range_m)
        return copernicus_dem, _copernicus_metadata(
            analysis_range_m, used_fallback=True, fallback_reason=metadata.fallback_reason
        )

    dem = open_copernicus_dem(lat, lon, analysis_range_m)
    return dem, _copernicus_metadata(analysis_range_m, used_fallback=False, fallback_reason=None)


def _try_cuzk(
    lat: float, lon: float, analysis_range_m: float, pixel_size_m: float
) -> tuple[DEMSource | None, DEMMetadata]:
    try:
        dem = open_cuzk_dmr5g_dem(lat, lon, analysis_range_m, pixel_size_m=pixel_size_m)
    except OutsideCoverageError as exc:
        return None, _failed_metadata(analysis_range_m, pixel_size_m, f"mimo pokrytí ČÚZK: {exc}")
    except (requests.exceptions.RequestException, RuntimeError) as exc:
        # RequestException pokrývá timeout/connection error/HTTPError (5xx apod.);
        # RuntimeError pokrývá "server vrátil JSON chybu místo obrázku" (viz app/cuzk_dem.py).
        return None, _failed_metadata(analysis_range_m, pixel_size_m, f"chyba požadavku na ČÚZK: {exc}")

    elevation = dem.elevation(lat, lon)
    if math.isnan(elevation):
        dem.close()
        return None, _failed_metadata(analysis_range_m, pixel_size_m, "NODATA na pozici pozorovatele v ČÚZK datech")

    lo, hi = CZ_PLAUSIBLE_ELEVATION_RANGE
    if not (lo <= elevation <= hi):
        dem.close()
        return None, _failed_metadata(
            analysis_range_m, pixel_size_m, f"podezřelá elevace {elevation:.1f}m mimo očekávaný rozsah ČR ({lo}-{hi}m)"
        )

    bbox = bbox_for_area(lat, lon, analysis_range_m)
    actual_pixel_size_m = effective_pixel_size(bbox, pixel_size_m)  # mohlo být zhrubeno, viz app/cuzk_dem.py

    metadata = DEMMetadata(
        provider=PROVIDER_CUZK,
        dataset="ČÚZK DMR 5G (DTM)",
        spatial_resolution_m=actual_pixel_size_m,
        analysis_range_m=analysis_range_m,
        source_crs="EPSG:5514 → EPSG:4326 (reprojekce serverem)",
        acquired_at=datetime.now(timezone.utc),
        used_fallback=False,
        fallback_reason=None,
        quality_flag="ok",
    )
    return dem, metadata


def _failed_metadata(analysis_range_m: float, pixel_size_m: float, reason: str) -> DEMMetadata:
    return DEMMetadata(
        provider=PROVIDER_CUZK,
        dataset="ČÚZK DMR 5G (DTM)",
        spatial_resolution_m=pixel_size_m,
        analysis_range_m=analysis_range_m,
        source_crs="EPSG:5514 → EPSG:4326 (reprojekce serverem)",
        acquired_at=None,
        used_fallback=True,
        fallback_reason=reason,
        quality_flag="suspicious" if "podezřelá" in reason else "ok",
    )


def _copernicus_metadata(analysis_range_m: float, used_fallback: bool, fallback_reason: str | None) -> DEMMetadata:
    return DEMMetadata(
        provider=PROVIDER_COPERNICUS,
        dataset="Copernicus GLO-30 (DSM)",
        spatial_resolution_m=30.0,
        analysis_range_m=analysis_range_m,
        source_crs="EPSG:4326",
        acquired_at=datetime.now(timezone.utc),
        used_fallback=used_fallback,
        fallback_reason=fallback_reason,
        quality_flag="ok",
    )
