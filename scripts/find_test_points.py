"""Krok 3: najde reprezentativní testovací body pro různé terénní situace
PROGRAMOVĚ přímo z DEM dat (ne odhadem souřadnic z paměti - to už jednou
způsobilo chybu u Milešovky, viz PLAN.md bod 10). Klasifikace je založená
na měřitelné lokální reliéfní charakteristice, ne na názvu místa.

Spouštět z kořene projektu po aktivaci .venv:

    python scripts/find_test_points.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import rasterio

from app.geo import destination_point

TILES = {
    "milesovka": str(Path(__file__).resolve().parent.parent / "data" / "milesovka_dem.tif"),
    "krusne_hory": str(Path(__file__).resolve().parent.parent / "data" / "krusne_hory_dem.tif"),
}


def local_relief_stats(band: np.ndarray, row: int, col: int, radius_px: int):
    """Vrátí (elevation, relief_position, gradient_ns, gradient_ew) pro
    pixel (row,col): `relief_position` je 0.0 (lokální minimum okolí) až
    1.0 (lokální maximum okolí) - škáluje nezávisle na absolutní výšce."""
    r0, r1 = max(0, row - radius_px), min(band.shape[0], row + radius_px + 1)
    c0, c1 = max(0, col - radius_px), min(band.shape[1], col + radius_px + 1)
    window = band[r0:r1, c0:c1]

    elevation = float(band[row, col])
    w_min, w_max = float(window.min()), float(window.max())
    relief_position = (elevation - w_min) / (w_max - w_min) if w_max > w_min else 0.5

    # hrubý gradient (m/pixel) v obou osách pro odhad "hřebenovitosti"
    gradient_ns = float(band[min(row + radius_px, band.shape[0] - 1), col] - band[max(row - radius_px, 0), col])
    gradient_ew = float(band[row, min(col + radius_px, band.shape[1] - 1)] - band[row, max(col - radius_px, 0)])

    return elevation, relief_position, gradient_ns, gradient_ew


def classify_grid(band: np.ndarray, transform, radius_px: int = 10, stride: int = 15):
    """Projde DEM v mřížce (kvůli výkonu ne pixel po pixelu) a spočítá
    reliéfní statistiky pro každý bod - vrátí seznam kandidátů s jejich
    (lat, lon, elevation, relief_position, ridge_score)."""
    candidates = []
    n_rows, n_cols = band.shape
    for row in range(radius_px, n_rows - radius_px, stride):
        for col in range(radius_px, n_cols - radius_px, stride):
            elevation, relief_position, grad_ns, grad_ew = local_relief_stats(band, row, col, radius_px)
            if np.isnan(elevation):
                continue
            lon, lat = transform * (col + 0.5, row + 0.5)
            # "ridge_score": vysoký, pokud je bod výrazně výš než okolí V JEDNOM
            # směru (silný gradient), ale relativně stabilní v kolmém směru
            # (typický profil pro hřeben, na rozdíl od izolovaného vrcholu,
            # který stoupá/klesá silně VE VŠECH směrech).
            ridge_score = max(abs(grad_ns), abs(grad_ew)) - min(abs(grad_ns), abs(grad_ew))
            candidates.append(
                {
                    "lat": lat,
                    "lon": lon,
                    "elevation": elevation,
                    "relief_position": relief_position,
                    "ridge_score": ridge_score,
                }
            )
    return candidates


def main() -> None:
    all_points = {}

    for name, path in TILES.items():
        with rasterio.open(path) as ds:
            band = ds.read(1)
            transform = ds.transform

        candidates = classify_grid(band, transform)
        candidates = [c for c in candidates if not np.isnan(c["elevation"])]

        # vrchol: mezi lokálními maximy (relief_position blízko 1) vezmi to
        # s NEJVYŠŠÍ absolutní nadmořskou výškou (ne první nalezené - jinak
        # to skončí na nevýrazném kopečku, který náhodou vyhrál remízu).
        peak = max(candidates, key=lambda c: c["elevation"] if c["relief_position"] > 0.9 else -1)
        # údolí: analogicky mezi lokálními minimy nejnižší výška.
        valley = min(candidates, key=lambda c: c["elevation"] if c["relief_position"] < 0.1 else 1e9)
        # svah: relief_position blízko 0.5 (ani vrchol, ani dno) A NÍZKÁ
        # asymetrie gradientu (rovnoměrný sklon, ne sedlo/hrana) - proto
        # MINIMALIZUJEME ridge_score, ne maximalizujeme.
        slope_candidates = [c for c in candidates if 0.35 < c["relief_position"] < 0.65]
        slope = min(slope_candidates, key=lambda c: c["ridge_score"]) if slope_candidates else None
        # hřeben: vysoký relief_position (je nad okolím) A VYSOKÉ ridge_score
        # - na rozdíl od izolovaného vrcholu (strmý pokles souměrně na
        # všechny strany -> nízké ridge_score) hřeben klesá strmě JEN
        # kolmo na svůj směr, podél hřbetu zůstává relativně plochý.
        high_candidates = [c for c in candidates if c["relief_position"] > 0.75]
        ridge = max(high_candidates, key=lambda c: c["ridge_score"]) if high_candidates else None

        all_points[name] = {"peak": peak, "valley": valley, "slope": slope, "ridge": ridge}

        print(f"=== {name} ===")
        for label, pt in all_points[name].items():
            if pt is None:
                print(f"  {label}: nenalezeno")
                continue
            print(
                f"  {label:8s}: {pt['lat']:.4f}, {pt['lon']:.4f}  elev={pt['elevation']:6.1f}m  "
                f"relief_pos={pt['relief_position']:.2f}  ridge_score={pt['ridge_score']:.1f}"
            )
        print()

    return all_points


if __name__ == "__main__":
    main()
