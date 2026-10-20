"""Načtení DEM (Digital Elevation Model) a bilineární sampling nadmořské
výšky pro libovolný GPS bod.

Zdroj-agnostické: dnes Copernicus DEM GLO-30 (viz PLAN.md - zdarma, bez
API klíče, S3), později případně ČÚZK DMR 5G (5m, přesnější, ale
distribuovaný jako LAZ mračno bodů nebo přes ArcGIS ImageServer - vyžaduje
extra krok před tím, než z něj bude stejný GeoTIFF ve WGS84 jako tady).

Pro první PoC se celá dlaždice (jednotky-desítky MB) načte do paměti -
u produkčního nasazení s dlaždicováním/cachováním napříč více dlaždicemi
(viz PLAN.md, bod 12) by se muselo přejít na oknové (windowed) čtení."""

from __future__ import annotations

import numpy as np
import rasterio


class OutOfBoundsError(ValueError):
    """Dotaz na bod mimo pokrytí této DEM dlaždice."""


class DEMSource:
    def __init__(self, path: str):
        self._dataset = rasterio.open(path)
        if self._dataset.crs is None or self._dataset.crs.to_epsg() != 4326:
            raise ValueError(
                f"DEMSource očekává rastr v EPSG:4326 (WGS84 lat/lon), ale {path} má CRS {self._dataset.crs}. "
                "Pro DEM v jiné projekci (např. ČÚZK S-JTSK/EPSG:5514) je potřeba nejdřív reprojekce (gdalwarp)."
            )
        self._band = self._dataset.read(1)
        self._transform = self._dataset.transform
        self._inv_transform = ~self._transform
        self._nodata = self._dataset.nodata
        # AUDIT NÁLEZ (Krok 8, profiling): `rasterio` dataset `.bounds`
        # přepočítává Affine transformaci z GDAL při KAŽDÉM přístupu -
        # `contains()` se volá jednou na každý krok paprsku (statisíce za
        # 360° horizont), takže tohle bylo ~70 % celého výpočetního času.
        # Hranice se za dobu života otevřeného souboru nemění - stačí je
        # spočítat jednou tady.
        self._bounds = self._dataset.bounds

    @property
    def bounds(self):
        return self._bounds

    def contains(self, lat: float, lon: float) -> bool:
        b = self.bounds
        return b.left <= lon <= b.right and b.bottom <= lat <= b.top

    def elevation(self, lat: float, lon: float) -> float:
        """Bilineárně interpolovaná nadmořská výška (m) v daném bodě.

        AUDIT NÁLEZ (Krok 1): dřív tahle metoda nekontrolovala hranice
        dlaždice vůbec - bod mimo pokrytí se tiše "clampnul" na okrajový
        pixel a vrátil se jako by to byla platná hodnota. To je nebezpečné
        - jakýkoliv budoucí kód volající elevation() bez vlastní kontroly
        `contains()` by dostal tiše špatná data místo chyby. Teď se mimo
        hranice hází `OutOfBoundsError` a NODATA pixely vrací NaN (viz
        `_bilinear_sample`) - volající (např. `app/horizon.py`) na to musí
        reagovat explicitně, ne dostat čísla, která vypadají důvěryhodně,
        ale nejsou."""
        if not self.contains(lat, lon):
            raise OutOfBoundsError(f"Bod ({lat}, {lon}) je mimo hranice DEM {self.bounds}")
        col, row = self._inv_transform @ (lon, lat)
        return _bilinear_sample(self._band, row, col, self._nodata)

    def close(self) -> None:
        self._dataset.close()

    def __enter__(self) -> "DEMSource":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def _bilinear_sample(band: np.ndarray, row: float, col: float, nodata: float | None) -> float:
    n_rows, n_cols = band.shape

    row0 = int(np.floor(row))
    col0 = int(np.floor(col))
    row0_clamped = max(0, min(row0, n_rows - 1))
    col0_clamped = max(0, min(col0, n_cols - 1))
    row1 = min(row0_clamped + 1, n_rows - 1)
    col1 = min(col0_clamped + 1, n_cols - 1)

    fr = min(max(row - row0, 0.0), 1.0)
    fc = min(max(col - col0, 0.0), 1.0)

    v00 = band[row0_clamped, col0_clamped]
    v01 = band[row0_clamped, col1]
    v10 = band[row1, col0_clamped]
    v11 = band[row1, col1]

    if nodata is not None and nodata in (v00, v01, v10, v11):
        # Radši jasně "nevím" (NaN) než tiše zprůměrovat NODATA sentinel
        # (typicky obří záporné číslo jako -32767) do "věrohodně" vypadající
        # nadmořské výšky.
        return float("nan")

    top = v00 * (1 - fc) + v01 * fc
    bottom = v10 * (1 - fc) + v11 * fc
    return float(top * (1 - fr) + bottom * fr)
