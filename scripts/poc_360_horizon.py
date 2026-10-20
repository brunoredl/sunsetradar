"""Krok 2: 360° terénní horizont (celý kruh, ne jen západní sektor) +
panoramatická (polární) vizualizace s vyznačeným azimutem západu Slunce.

Spouštět z kořene projektu, po aktivaci .venv a stažení DEM (viz README.md):

    python scripts/poc_360_horizon.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import matplotlib.pyplot as plt
import numpy as np

from app.dem import DEMSource
from app.horizon import compute_horizon_profile
from app.solar import astronomical_sunset, make_observer, sun_position

OBSERVER_LAT = 50.5552
OBSERVER_LON = 13.9312
OBSERVER_EYE_HEIGHT_M = 1.7

DEM_PATH = str(Path(__file__).resolve().parent.parent / "data" / "milesovka_dem.tif")

AZIMUTH_STEP_DEG = 1.0
MAX_DISTANCE_M = 30_000.0
STEP_M = 90.0

from datetime import date

TARGET_DATE = date(2026, 9, 22)


def main() -> None:
    timings: dict[str, float] = {}

    t0 = time.perf_counter()
    with DEMSource(DEM_PATH) as dem:
        timings["dem_load_s"] = time.perf_counter() - t0

        observer_elevation = dem.elevation(OBSERVER_LAT, OBSERVER_LON)
        print(f"Pozorovatel: {OBSERVER_LAT:.4f}, {OBSERVER_LON:.4f} ({observer_elevation:.1f} m)")

        print(f"Počítám 360° horizont (krok {AZIMUTH_STEP_DEG:.0f}°, dosah {MAX_DISTANCE_M/1000:.0f} km)...")
        t1 = time.perf_counter()
        profile = compute_horizon_profile(
            dem,
            OBSERVER_LAT,
            OBSERVER_LON,
            OBSERVER_EYE_HEIGHT_M,
            azimuth_start_deg=0.0,
            azimuth_end_deg=360.0 - AZIMUTH_STEP_DEG,
            azimuth_step_deg=AZIMUTH_STEP_DEG,
            max_distance_m=MAX_DISTANCE_M,
            step_m=STEP_M,
        )
        timings["horizon_360_s"] = time.perf_counter() - t1

    n_nan = int(np.isnan(profile.horizon_altitude_deg).sum())
    n_rays = len(profile.azimuths_deg)
    print(f"Hotovo: {n_rays} azimutů, {n_nan} bez dat (NaN).")
    print(f"  DEM load:        {timings['dem_load_s']*1000:.1f} ms")
    print(f"  360° horizont:   {timings['horizon_360_s']*1000:.1f} ms ({timings['horizon_360_s']/n_rays*1000:.2f} ms/azimut)")

    observer = make_observer(OBSERVER_LAT, OBSERVER_LON, observer_elevation + OBSERVER_EYE_HEIGHT_M)
    sunset_time = astronomical_sunset(observer, TARGET_DATE)
    sunset_pos = sun_position(observer, sunset_time)

    print()
    print(f"Azimut západu Slunce: {sunset_pos.azimuth_deg:.1f}°")
    print(f"Terénní horizont tam: {profile.altitude_at(sunset_pos.azimuth_deg):+.2f}°")

    valid = ~np.isnan(profile.horizon_altitude_deg)
    print(f"Rozsah horizontu (platné vzorky): {profile.horizon_altitude_deg[valid].min():+.2f}° "
          f"až {profile.horizon_altitude_deg[valid].max():+.2f}°")

    _plot_cartesian(profile, sunset_pos.azimuth_deg)
    _plot_polar(profile, sunset_pos.azimuth_deg)


def _plot_cartesian(profile, sunset_azimuth_deg: float) -> None:
    fig, ax = plt.subplots(figsize=(12, 4))
    ax.plot(profile.azimuths_deg, profile.horizon_altitude_deg, color="#8B4513", linewidth=1.2)
    ax.axhline(0, color="gray", linewidth=0.8, linestyle="--")
    ax.axvline(sunset_azimuth_deg, color="orange", linewidth=1.2, linestyle=":", label=f"Západ Slunce ({sunset_azimuth_deg:.0f}°)")
    ax.fill_between(profile.azimuths_deg, profile.horizon_altitude_deg, -5, color="#8B4513", alpha=0.15)
    ax.set_xlim(0, 360)
    ax.set_xticks(range(0, 361, 30))
    ax.set_xlabel("Azimut (°)")
    ax.set_ylabel("Výškový úhel (°)")
    ax.set_title("360° terénní horizont - Milešovka")
    ax.legend(loc="upper right", fontsize=8)
    ax.grid(True, alpha=0.3)

    out_path = Path(__file__).resolve().parent.parent / "data" / "horizon_360_cartesian.png"
    fig.savefig(out_path, dpi=120, bbox_inches="tight")
    print(f"Graf uložen: {out_path}")


def _plot_polar(profile, sunset_azimuth_deg: float) -> None:
    """Panoramatický (polární) diagram - stejná konvence jako u nástrojů
    pro posouzení oslunění (Solar Pathfinder/SunEye): azimut po kompasu
    (sever nahoře, po směru hodinových ručiček), poloměr = zenitová
    vzdálenost (90° - výška), takže horizont (0°) je u vnějšího okraje a
    zenit (90°) uprostřed. Vystínovaná oblast = terén/obstrukce."""
    theta = np.radians(profile.azimuths_deg)
    radius = 90.0 - profile.horizon_altitude_deg  # zenitová vzdálenost

    fig, ax = plt.subplots(figsize=(7, 7), subplot_kw={"projection": "polar"})
    ax.set_theta_zero_location("N")
    ax.set_theta_direction(-1)  # po směru hodinových ručiček (kompas)

    ax.plot(theta, radius, color="#8B4513", linewidth=1.2)
    ax.fill_between(theta, radius, 90, color="#8B4513", alpha=0.35)

    sunset_theta = np.radians(sunset_azimuth_deg)
    ax.plot([sunset_theta, sunset_theta], [0, 90], color="orange", linewidth=1.5, linestyle=":", label=f"Západ Slunce ({sunset_azimuth_deg:.0f}°)")

    ax.set_rlim(0, 90)
    ax.set_rticks([0, 30, 60, 90])
    ax.set_yticklabels(["90° (zenit)", "60°", "30°", "0° (horizont)"], fontsize=7)
    ax.set_title("Panorama horizontu - Milešovka (360°)", pad=20)
    ax.legend(loc="lower right", bbox_to_anchor=(1.15, -0.05), fontsize=8)

    out_path = Path(__file__).resolve().parent.parent / "data" / "horizon_360_polar.png"
    fig.savefig(out_path, dpi=120, bbox_inches="tight")
    print(f"Graf uložen: {out_path}")


if __name__ == "__main__":
    main()
