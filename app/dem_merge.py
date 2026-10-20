"""Sdílená pomůcka pro sloučení víc GeoTIFF souborů do jedné mozaiky -
používá jak `app/tile_manager.py` (Copernicus), tak `app/cuzk_dem.py`
(ČÚZK DMR 5G), aby se stejná logika nepsala dvakrát."""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def merge_geotiffs(paths: list[Path], output_path: Path) -> str:
    """Sloučí `paths` (GeoTIFF, stejné CRS) do jedné mozaiky na
    `output_path`. Pokud už mozaika existuje, nepočítá se znovu."""
    import rasterio
    from rasterio.merge import merge

    if output_path.exists():
        logger.info("Sloučená mozaika %s už existuje v cache.", output_path)
        return str(output_path)

    logger.info("Slučuji %d souborů do mozaiky %s ...", len(paths), output_path)
    sources = [rasterio.open(p) for p in paths]
    try:
        mosaic, out_transform = merge(sources)
        meta = sources[0].meta.copy()
        meta.update({"height": mosaic.shape[1], "width": mosaic.shape[2], "transform": out_transform})
        with rasterio.open(output_path, "w", **meta) as dst:
            dst.write(mosaic)
    finally:
        for src in sources:
            src.close()

    return str(output_path)
