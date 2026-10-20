"""ČÚZK DMR 5G přes ArcGIS ImageServer `exportImage` - žádný API klíč,
žádné ruční GIS zpracování LAZ mračna bodů (viz Krok 1 tohohle auditu).
Server umí na požádání rovnou reprojektovat výstup do EPSG:4326 (WGS84) -
stejné CRS jako Copernicus DEM, takže `app/dem.py::DEMSource` otevře
výsledek úplně stejně, beze změny.

AUDIT NÁLEZ (Krok 6, měřeno, ne odhadnuto): první verze dláždila oblast do
vlastní virtuální mřížky malých buněk (0.25°) a slučovala je - pro 30km
poloměr to znamenalo 12 samostatných HTTP požadavků a **206,71 sekund**
celkem (~17s/buňka). To je 40-60× pomalejší než Copernicus (jedna dlaždice
z S3, ~3-6s). Přepsáno na JEDEN přímý požadavek na přesně potřebný bbox -
ArcGIS ImageServer limituje jeden export na `maxImageWidth=15000,
maxImageHeight=4100` pixelů (zjištěno z metadat služby), takže při
zvoleném rozlišení buď (a) požadovaná oblast do limitu spadá -> 1 request,
nebo (b) je moc velká -> rozlišení se automaticky zhrubí (ne dláždí), aby
pořád stačil jeden request. Dláždění/merge zůstává k dispozici
(`merge_geotiffs`) pro budoucí použití, ale není to výchozí cesta.

Licence: CC BY 4.0 (ČÚZK open data), komerční využití povoleno, nutná
atribuce "© ČÚZK"."""

from __future__ import annotations

import hashlib
import logging
import math
from pathlib import Path

import requests

logger = logging.getLogger(__name__)

IMAGESERVER_EXPORT_URL = "https://ags.cuzk.gov.cz/arcgis2/rest/services/dmr5g/ImageServer/exportImage"
DEFAULT_CACHE_DIR = Path(__file__).resolve().parent.parent / "data" / "cuzk_cache"

DEFAULT_PIXEL_SIZE_M = 10.0  # 3x jemnější než Copernicus (30m); nativně má ČÚZK ~2m k dispozici
MAX_IMAGE_WIDTH_PX = 15000  # zdokumentováno v metadatech ImageServeru (?f=json)
MAX_IMAGE_HEIGHT_PX = 4100  # zdokumentováno v metadatech ImageServeru (?f=json)
# NEZDOKUMENTOVANÝ nález (Krok 6, empiricky proměřeno binárním hledáním):
# server vrátí 500 Internal Server Error nad ~9-10 milionů pixelů CELKEM,
# výrazně dřív, než by dovolily samotné maxImageWidth/Height (ty by
# povolily až 61,5M px). Testováno: 3000x3000 (9M px) OK za 27s,
# 3200x3200 (10,24M px) selhalo okamžitě (500, ~8s, žádný render).
# Bezpečná rezerva pod touhle nezdokumentovanou hranicí:
MAX_TOTAL_PIXELS = 8_000_000
_METERS_PER_DEGREE_LAT = 111_320.0

# ČÚZK data pokrývají jen ČR - hrubý obalový obdélník pro rychlou kontrolu
# (přesnou hranici republiky neřešíme, stačí odmítnout zjevně mimo-ČR body).
CZ_BBOX = (12.0, 48.5, 19.0, 51.1)  # (west, south, east, north)


class OutsideCoverageError(ValueError):
    """Bod je mimo pokrytí ČÚZK DMR 5G (mimo Českou republiku)."""


def is_roughly_in_cz(lat: float, lon: float) -> bool:
    west, south, east, north = CZ_BBOX
    return west <= lon <= east and south <= lat <= north


def bbox_for_area(lat: float, lon: float, radius_m: float) -> tuple[float, float, float, float]:
    """Vrátí (west, south, east, north) obalový obdélník kolem kruhu o
    daném poloměru - stejný konzervativní přístup jako `app/tile_manager.py`."""
    lat_margin = radius_m / _METERS_PER_DEGREE_LAT
    lon_margin = radius_m / (_METERS_PER_DEGREE_LAT * max(math.cos(math.radians(lat)), 0.01))
    return lon - lon_margin, lat - lat_margin, lon + lon_margin, lat + lat_margin


def pixel_dims_for_bbox(bbox: tuple[float, float, float, float], pixel_size_m: float) -> tuple[int, int]:
    west, south, east, north = bbox
    center_lat = (south + north) / 2
    width_m = (east - west) * _METERS_PER_DEGREE_LAT * math.cos(math.radians(center_lat))
    height_m = (north - south) * _METERS_PER_DEGREE_LAT
    return max(1, round(width_m / pixel_size_m)), max(1, round(height_m / pixel_size_m))


def effective_pixel_size(bbox: tuple[float, float, float, float], requested_pixel_size_m: float) -> float:
    """Pokud by požadovaná oblast při `requested_pixel_size_m` přesáhla
    zdokumentované (šířka/výška) NEBO empiricky zjištěné nezdokumentované
    (celkový počet pixelů, viz MAX_TOTAL_PIXELS) limity ImageServeru,
    zhrubí rozlišení JEN NATOLIK, aby se vešla do jednoho požadavku."""
    width_px, height_px = pixel_dims_for_bbox(bbox, requested_pixel_size_m)
    total_px = width_px * height_px

    scale_dims = max(width_px / MAX_IMAGE_WIDTH_PX, height_px / MAX_IMAGE_HEIGHT_PX, 1.0)
    scale_total = max((total_px / MAX_TOTAL_PIXELS) ** 0.5, 1.0)  # odmocnina, protože škáluje plochu
    scale = max(scale_dims, scale_total)

    if scale > 1.0:
        adjusted = requested_pixel_size_m * scale * 1.02  # malá rezerva proti zaokrouhlení
        logger.warning(
            "Požadovaná oblast při %.1fm/px přesahuje limity ImageServeru "
            "(%dx%d px dimenze, %d px celkem) - rozlišení zhrubeno na %.1fm/px.",
            requested_pixel_size_m, MAX_IMAGE_WIDTH_PX, MAX_IMAGE_HEIGHT_PX, MAX_TOTAL_PIXELS, adjusted,
        )
        return adjusted
    return requested_pixel_size_m


class CuzkDMR5GCache:
    def __init__(self, cache_dir: str | Path = DEFAULT_CACHE_DIR):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def _bbox_cache_path(self, bbox: tuple[float, float, float, float], pixel_size_m: float) -> Path:
        key = f"{bbox[0]:.5f}_{bbox[1]:.5f}_{bbox[2]:.5f}_{bbox[3]:.5f}_{pixel_size_m:.2f}"
        digest = hashlib.sha1(key.encode()).hexdigest()[:16]
        return self.cache_dir / f"cuzk_{digest}.tif"

    def ensure_local_bbox(
        self, bbox: tuple[float, float, float, float], pixel_size_m: float = DEFAULT_PIXEL_SIZE_M, timeout_s: float = 120.0
    ) -> Path:
        """Stáhne (nebo vrátí z cache) GeoTIFF pro přesně daný bbox -
        JEDEN HTTP požadavek, žádné dláždění (viz docstring modulu)."""
        path = self._bbox_cache_path(bbox, pixel_size_m)
        if path.exists():
            logger.info("ČÚZK bbox %s @ %.1fm/px už je v cache.", bbox, pixel_size_m)
            return path

        width_px, height_px = pixel_dims_for_bbox(bbox, pixel_size_m)
        west, south, east, north = bbox
        params = {
            "bbox": f"{west},{south},{east},{north}",
            "bboxSR": 4326,
            "imageSR": 4326,
            "size": f"{width_px},{height_px}",
            "format": "tiff",
            "pixelType": "F32",
            "interpolation": "RSP_BilinearInterpolation",
            "f": "image",
        }
        logger.info("Stahuji ČÚZK bbox %s (%dx%d px @ %.1fm/px) ...", bbox, width_px, height_px, pixel_size_m)
        response = requests.get(IMAGESERVER_EXPORT_URL, params=params, timeout=timeout_s)
        response.raise_for_status()
        if response.headers.get("Content-Type", "").startswith("application/json"):
            # ArcGIS vrací chybu jako JSON i s HTTP 200 - nezaměnit za platný TIFF.
            raise RuntimeError(f"ČÚZK ImageServer vrátil chybu místo obrázku: {response.text[:500]}")

        tmp_path = path.with_suffix(".tif.part")
        tmp_path.write_bytes(response.content)
        tmp_path.rename(path)
        return path


def get_dem_path_for_area(
    lat: float,
    lon: float,
    radius_m: float,
    pixel_size_m: float = DEFAULT_PIXEL_SIZE_M,
    cache: CuzkDMR5GCache | None = None,
) -> str:
    """Analogie `app/tile_manager.py::get_dem_path_for_area`, jen pro
    ČÚZK DMR 5G - vrací cestu k lokálnímu GeoTIFF (EPSG:4326), který jde
    rovnou otevřít `app.dem.DEMSource`. Funguje jen pro body v ČR."""
    if not is_roughly_in_cz(lat, lon):
        raise OutsideCoverageError(f"Bod ({lat}, {lon}) je mimo přibližné pokrytí ČÚZK DMR 5G (Česká republika).")

    cache = cache or CuzkDMR5GCache()
    bbox = bbox_for_area(lat, lon, radius_m)
    effective_pixel_size_m = effective_pixel_size(bbox, pixel_size_m)
    path = cache.ensure_local_bbox(bbox, effective_pixel_size_m)
    return str(path)
