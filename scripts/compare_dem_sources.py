"""Krok 3-4 (DEM kvalita): porovná Copernicus GLO-30 vs. ČÚZK DMR 5G na
síti testovacích bodů z předchozí fáze - stejný bod, stejný azimut
západu, dva různé DEM zdroje. Cílem NENÍ jen konstatovat "DMR 5G je
přesnější", ale změřit KOLIK se liší, KDE nejvíc, a jestli to mění
praktický výsledek (čas zakrytí Slunce).

Spouštět z kořene projektu po aktivaci .venv:

    python scripts/compare_dem_sources.py
"""

from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.dem import DEMSource
from app.dem_provider import open_copernicus_dem, open_cuzk_dmr5g_dem
from app.horizon import compute_horizon_profile
from app.solar import make_observer
from app.visibility import estimate_sunset_visibility

TARGET_DATE = date(2026, 9, 22)
RADIUS_M = 30_000.0
AZIMUTH_STEP_DEG = 2.0
STEP_M = 90.0

TEST_POINTS = {
    "A - vrchol": (50.5552, 13.9312),
    "B - hřeben": (50.3972, 12.9479),
    "C - svah": (50.6556, 13.9417),
    "D - údolí": (50.5764, 13.7167),
    "E - blokováno": (50.5537, 13.9337),
}


def analyze(dem: DEMSource, lat: float, lon: float) -> dict:
    elevation = dem.elevation(lat, lon)
    profile = compute_horizon_profile(dem, lat, lon, 1.7, 0, 359, AZIMUTH_STEP_DEG, RADIUS_M, STEP_M)
    observer = make_observer(lat, lon, elevation + 1.7)
    visibility = estimate_sunset_visibility(observer, TARGET_DATE, profile, search_window_before=timedelta(hours=3))

    horizon_at_sunset = profile.altitude_at(visibility.astronomical_sunset_azimuth_deg)
    delay_min = 0.0
    if visibility.terrain_obstructed:
        delay_min = (visibility.astronomical_sunset_time - visibility.visible_until_time).total_seconds() / 60.0

    return {
        "elevation": elevation,
        "horizon_at_sunset": horizon_at_sunset,
        "sunset_azimuth": visibility.astronomical_sunset_azimuth_deg,
        "obstructed": visibility.terrain_obstructed,
        "delay_min": delay_min,
        "profile": profile,
    }


def main() -> None:
    rows = []

    for label, (lat, lon) in TEST_POINTS.items():
        print(f"=== {label} ({lat:.4f}, {lon:.4f}) ===")

        glo30_dem = open_copernicus_dem(lat, lon, RADIUS_M)
        glo30 = analyze(glo30_dem, lat, lon)
        glo30_dem.close()
        print(f"  GLO-30: elev={glo30['elevation']:.1f}m horizon={glo30['horizon_at_sunset']:+.2f}° delay={glo30['delay_min']:.1f}min")

        dmr5g_dem = open_cuzk_dmr5g_dem(lat, lon, RADIUS_M)
        dmr5g = analyze(dmr5g_dem, lat, lon)
        dmr5g_dem.close()
        print(f"  DMR 5G: elev={dmr5g['elevation']:.1f}m horizon={dmr5g['horizon_at_sunset']:+.2f}° delay={dmr5g['delay_min']:.1f}min")

        diff = dmr5g["horizon_at_sunset"] - glo30["horizon_at_sunset"]
        print(f"  Rozdíl horizontu: {diff:+.2f}°  |  Rozdíl elevace pozorovatele: {dmr5g['elevation']-glo30['elevation']:+.1f}m")
        print()

        rows.append(
            {
                "label": label,
                "glo30_horizon": glo30["horizon_at_sunset"],
                "dmr5g_horizon": dmr5g["horizon_at_sunset"],
                "diff": diff,
                "glo30_obstructed": glo30["obstructed"],
                "dmr5g_obstructed": dmr5g["obstructed"],
                "glo30_delay": glo30["delay_min"],
                "dmr5g_delay": dmr5g["delay_min"],
                "elev_diff": dmr5g["elevation"] - glo30["elevation"],
            }
        )

    print("\n" + "=" * 100)
    print(f"| {'Místo':15s} | {'GLO-30 horizon':>14s} | {'DMR5G horizon':>14s} | {'rozdíl':>8s} | {'GLO obstr.':>10s} | {'DMR obstr.':>10s} |")
    print("|" + "-" * 17 + "|" + "-" * 16 + "|" + "-" * 16 + "|" + "-" * 10 + "|" + "-" * 12 + "|" + "-" * 12 + "|")
    for r in rows:
        print(
            f"| {r['label']:15s} | {r['glo30_horizon']:14.2f} | {r['dmr5g_horizon']:14.2f} | {r['diff']:+8.2f} | "
            f"{str(r['glo30_obstructed']):>10s} | {str(r['dmr5g_obstructed']):>10s} |"
        )

    print("\nZpoždění západu (min):")
    for r in rows:
        print(f"  {r['label']:15s}  GLO-30={r['glo30_delay']:6.1f}min  DMR5G={r['dmr5g_delay']:6.1f}min  rozdíl={r['dmr5g_delay']-r['glo30_delay']:+.1f}min")


if __name__ == "__main__":
    main()
