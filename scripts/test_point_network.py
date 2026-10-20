"""Krok 3: síť testovacích bodů v různých terénních situacích - ověřuje,
že algoritmus dává rozumné a ODLIŠNÉ výsledky napříč terénem, ne jen na
jednom vybraném bodě (Milešovka).

Body byly nalezeny PROGRAMOVĚ z DEM dat (viz scripts/find_test_points.py),
ne odhadnuté z paměti - viz PLAN.md bod 10 pro důvod (nepřesná souřadnice
u Milešovky dřív způsobila zdánlivý bug).

Spouštět z kořene projektu po aktivaci .venv:

    python scripts/test_point_network.py
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import matplotlib.pyplot as plt
import numpy as np

from datetime import timedelta

from app.dem import DEMSource
from app.horizon import compute_horizon_profile
from app.solar import astronomical_sunset, make_observer, sun_position
from app.visibility import estimate_sunset_visibility

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
MILESOVKA_DEM = str(DATA_DIR / "milesovka_dem.tif")
KRUSNE_HORY_DEM = str(DATA_DIR / "krusne_hory_dem.tif")

TARGET_DATE = date(2026, 9, 22)
AZIMUTH_STEP_DEG = 2.0  # o něco hrubší než Krok 2 - 5 bodů najednou, chceme to rychlé
MAX_DISTANCE_M = 30_000.0
STEP_M = 90.0

TEST_POINTS = {
    "A - vysoký otevřený vrchol (Milešovka)": {
        "dem": MILESOVKA_DEM,
        "lat": 50.5552,
        "lon": 13.9312,
    },
    "B - hřeben (Krušné hory)": {
        "dem": KRUSNE_HORY_DEM,
        "lat": 50.3972,
        "lon": 12.9479,
    },
    "C - svah": {
        "dem": MILESOVKA_DEM,
        "lat": 50.6556,
        "lon": 13.9417,
    },
    "D - údolí": {
        "dem": MILESOVKA_DEM,
        "lat": 50.5764,
        "lon": 13.7167,
    },
    "E - kopec přímo na západě (Milešovka - východní svah)": {
        "dem": MILESOVKA_DEM,
        "lat": 50.5537,
        "lon": 13.9337,
    },
}


def main() -> None:
    results = {}

    for label, cfg in TEST_POINTS.items():
        with DEMSource(cfg["dem"]) as dem:
            try:
                observer_elevation = dem.elevation(cfg["lat"], cfg["lon"])
            except Exception as exc:  # OutOfBoundsError apod.
                print(f"{label}: CHYBA - {exc}")
                continue

            if np.isnan(observer_elevation):
                print(f"{label}: pozorovatel na NODATA bodě, přeskakuji")
                continue

            profile = compute_horizon_profile(
                dem,
                cfg["lat"],
                cfg["lon"],
                observer_eye_height_m=1.7,
                azimuth_start_deg=0.0,
                azimuth_end_deg=360.0 - AZIMUTH_STEP_DEG,
                azimuth_step_deg=AZIMUTH_STEP_DEG,
                max_distance_m=MAX_DISTANCE_M,
                step_m=STEP_M,
            )

            observer = make_observer(cfg["lat"], cfg["lon"], observer_elevation + 1.7)
            sunset_time = astronomical_sunset(observer, TARGET_DATE)
            sunset_pos = sun_position(observer, sunset_time)
            horizon_at_sunset = profile.altitude_at(sunset_pos.azimuth_deg)

            visibility = estimate_sunset_visibility(
                observer, TARGET_DATE, profile, search_window_before=timedelta(minutes=180), search_window_after=timedelta(minutes=10)
            )
            delay_min = (sunset_time - visibility.visible_until_time).total_seconds() / 60.0

            valid = ~np.isnan(profile.horizon_altitude_deg)
            n_nan = int((~valid).sum())

            results[label] = {
                "elevation": observer_elevation,
                "profile": profile,
                "sunset_azimuth": sunset_pos.azimuth_deg,
                "horizon_at_sunset": horizon_at_sunset,
                "n_nan": n_nan,
                "min": float(profile.horizon_altitude_deg[valid].min()) if valid.any() else float("nan"),
                "max": float(profile.horizon_altitude_deg[valid].max()) if valid.any() else float("nan"),
                "obstructed": visibility.terrain_obstructed,
                "delay_min": delay_min,
            }

            print(f"=== {label} ===")
            print(f"  Souřadnice: {cfg['lat']:.4f}, {cfg['lon']:.4f}  ({observer_elevation:.1f} m)")
            print(f"  Horizont rozsah: {results[label]['min']:+.2f}° až {results[label]['max']:+.2f}°  ({n_nan} NaN vzorků)")
            print(f"  Azimut západu: {sunset_pos.azimuth_deg:.1f}°  |  Horizont tam: {horizon_at_sunset:+.2f}°")
            if visibility.terrain_obstructed:
                print(f"  Slunce zmizí za terénem {delay_min:.1f} min PŘED astronomickým západem.")
            else:
                print("  Terén tenhle západ neomezuje nad rámec standardní definice.")
            print()

    _plot_comparison(results)


def _plot_comparison(results: dict) -> None:
    fig, axes = plt.subplots(len(results), 1, figsize=(11, 2.3 * len(results)), sharex=True)
    if len(results) == 1:
        axes = [axes]

    for ax, (label, r) in zip(axes, results.items()):
        profile = r["profile"]
        ax.plot(profile.azimuths_deg, profile.horizon_altitude_deg, color="#8B4513", linewidth=1.0)
        ax.axhline(0, color="gray", linewidth=0.6, linestyle="--")
        ax.axvline(r["sunset_azimuth"], color="orange", linewidth=1.0, linestyle=":")
        ax.fill_between(profile.azimuths_deg, profile.horizon_altitude_deg, -10, color="#8B4513", alpha=0.15)
        ax.set_ylabel(label, fontsize=7, rotation=0, ha="right", va="center")
        ax.set_xlim(0, 360)
        ax.grid(True, alpha=0.3)

    axes[-1].set_xlabel("Azimut (°)")
    axes[-1].set_xticks(range(0, 361, 30))
    fig.suptitle("Krok 3: síť testovacích bodů - 360° terénní horizont", fontsize=11)
    fig.tight_layout()

    out_path = DATA_DIR / "test_point_network.png"
    fig.savefig(out_path, dpi=120, bbox_inches="tight")
    print(f"Graf uložen: {out_path}")


if __name__ == "__main__":
    main()
