"""Proof-of-concept: pro daný GPS bod spočítá terénní horizont v západním
sektoru, polohu Slunce a odhadne, kdy Slunce skutečně zmizí za terénem
(ne jen astronomický čas západu). Viz PLAN.md pro princip a diskuzi
přesnosti/omezení.

Spouštět z kořene projektu (sunset-radar/), po aktivaci .venv:

    python scripts/poc_sunset.py

Testovací bod: Milešovka (837 m, České středohoří) - izolovaný vrchol se
známým širokým výhledem, dobrý sanity-check kandidát (viz PLAN.md, bod 11:
validace srovnáním se známou vyhlídkou).
"""

from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import matplotlib.pyplot as plt

from app.dem import DEMSource
from app.horizon import compute_horizon_profile
from app.solar import make_observer
from app.visibility import estimate_sunset_visibility

# --- Testovací bod: Milešovka ---
# POZOR: první verze používala souřadnice (50.5537, 13.9337) odhadnuté
# z paměti - ukázalo se, že leží ~160 m na východním svahu, ne na vrcholu
# (jemné vzorkování po 20 m odhalilo hladký, skutečný vrchol hory podél
# paprsku k západu - viz PLAN.md, sekce "Nalezené problémy"). Opraveno na
# souřadnice skutečného vrcholu nalezené jako lokální maximum v DEM.
OBSERVER_LAT = 50.5552
OBSERVER_LON = 13.9312
OBSERVER_TERRAIN_ELEVATION_HINT_M = 837.0  # veřejně známá výška (Wikipedie) - jen pro referenci v textu
OBSERVER_EYE_HEIGHT_M = 1.7  # výška očí stojícího pozorovatele nad terénem

DEM_PATH = str(Path(__file__).resolve().parent.parent / "data" / "milesovka_dem.tif")

TARGET_DATE = date(2026, 9, 22)  # dnešní datum v simulaci - podzimní rovnodennost, blízko

# Západní sektor + rozlišení (viz PLAN.md, bod 4)
AZIMUTH_START_DEG = 240.0
AZIMUTH_END_DEG = 300.0
AZIMUTH_STEP_DEG = 1.0

# Dosah analýzy terénu - 30 km je kompromis mezi zachycením vzdálených
# horských hřebenů (Krušné hory jsou ~35-45 km na západ/jihozápad) a
# výpočetním časem (viz PLAN.md, bod 4). Pro první PoC dostatečné na
# ověření principu, i když nejvzdálenější hřebeny může useknout.
MAX_DISTANCE_M = 30_000.0
STEP_M = 90.0  # ~3x DEM pixel (Copernicus GLO-30 ~30m) - viz PLAN.md, bod 12


def main() -> None:
    print(f"Pozorovatel: {OBSERVER_LAT:.4f}, {OBSERVER_LON:.4f} (Milešovka)")
    print(f"Datum: {TARGET_DATE.isoformat()}")
    print()

    with DEMSource(DEM_PATH) as dem:
        observer_terrain_elevation = dem.elevation(OBSERVER_LAT, OBSERVER_LON)
        print(f"Nadmořská výška z DEM: {observer_terrain_elevation:.1f} m " f"(referenční hodnota: {OBSERVER_TERRAIN_ELEVATION_HINT_M:.0f} m)")

        print(
            f"Počítám terénní horizont ({AZIMUTH_START_DEG:.0f}°-{AZIMUTH_END_DEG:.0f}°, "
            f"krok {AZIMUTH_STEP_DEG:.0f}°, dosah {MAX_DISTANCE_M/1000:.0f} km)..."
        )
        profile = compute_horizon_profile(
            dem,
            OBSERVER_LAT,
            OBSERVER_LON,
            OBSERVER_EYE_HEIGHT_M,
            AZIMUTH_START_DEG,
            AZIMUTH_END_DEG,
            AZIMUTH_STEP_DEG,
            MAX_DISTANCE_M,
            STEP_M,
        )

        observer = make_observer(OBSERVER_LAT, OBSERVER_LON, observer_terrain_elevation + OBSERVER_EYE_HEIGHT_M)
        result = estimate_sunset_visibility(
            observer,
            TARGET_DATE,
            profile,
            search_window_before=timedelta(minutes=120),
            search_window_after=timedelta(minutes=30),
        )

    local_sunset = result.astronomical_sunset_time.astimezone()
    local_visible_until = result.visible_until_time.astimezone()

    print()
    print("=== Výsledek ===")
    print(f"Observer:                 {result.observer_lat:.4f}, {result.observer_lon:.4f}")
    print(f"Astronomical sunset:      {local_sunset.strftime('%H:%M:%S')}")
    print(f"Sunset azimuth:           {result.astronomical_sunset_azimuth_deg:.1f}°")
    print(f"Sun altitude at sunset:   {result.astronomical_sunset_sun_altitude_deg:+.2f}°")
    print(f"Terrain horizon (azimut): {result.terrain_horizon_at_sunset_azimuth_deg:+.2f}°")
    print(f"Terrain obstruction:      {'ANO' if result.terrain_obstructed else 'ne'}")
    print(f"Estimated visible until:  {local_visible_until.strftime('%H:%M:%S')}")

    if result.terrain_obstructed:
        delta = result.astronomical_sunset_time - result.visible_until_time
        print(f"  -> Slunce zmizí za terénem o {delta.total_seconds()/60:.1f} minut dřív, než by zapadlo nad rovinou.")
    else:
        print("  -> Terén tenhle západ neomezuje - platí standardní (astronomický) čas.")

    _plot(profile, result)


def _plot(profile, result) -> None:
    fig, ax = plt.subplots(figsize=(10, 5))
    ax.plot(profile.azimuths_deg, profile.horizon_altitude_deg, color="#8B4513", linewidth=1.5, label="Terénní horizont")
    ax.axhline(0, color="gray", linewidth=0.8, linestyle="--", label="Astronomický horizont (0°)")
    ax.axvline(
        result.astronomical_sunset_azimuth_deg,
        color="orange",
        linewidth=1.5,
        linestyle=":",
        label=f"Azimut západu ({result.astronomical_sunset_azimuth_deg:.0f}°)",
    )
    ax.fill_between(profile.azimuths_deg, profile.horizon_altitude_deg, -5, color="#8B4513", alpha=0.2)

    ax.set_xlabel("Azimut (°)")
    ax.set_ylabel("Výškový úhel (°)")
    ax.set_title(f"Terénní horizont - Milešovka, západní sektor ({result.observer_lat:.3f}, {result.observer_lon:.3f})")
    ax.legend(loc="upper right", fontsize=8)
    ax.grid(True, alpha=0.3)

    out_path = Path(__file__).resolve().parent.parent / "data" / "horizon_profile.png"
    fig.savefig(out_path, dpi=120, bbox_inches="tight")
    print(f"\nGraf uložen: {out_path}")


if __name__ == "__main__":
    main()
