"""Fáze 8 (rozšířeno Fáze 11 o víc regionů) - offline dávkový skript:
discover_candidates_cached() pro daný region -> filtrace/dedup ->
analyze_candidates() (SEKVENČNĚ, s reálným měřením času na kandidáta)
-> rank_by_rating() -> zápis do data/candidates_<region>.json.

DŮLEŽITÉ (zadání bod 14): tenhle skript se spouští RUČNĚ
(`python scripts/build_candidate_dataset.py [region ...]`, bez
argumentů = všechny regiony), NIKDY z HTTP request handleru - terrain
analýza desítek bodů by u živého requestu vedla k timeoutu (viz
naměřené časy v PLAN.md Fáze 8). Webová vrstva (GET /api/candidates)
jen čte výstupní JSON."""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.candidate_batch import analyze_candidates, rank_by_rating
from app.candidate_discovery import CZECHIA_BBOX, deduplicate, discover_candidates_cached, filter_within_bbox
from app.regions import REGIONS

SAMPLE_SIZE = 12  # zadání bod 11 - "nejdřív prototyp na malé oblasti", neškálovat naslepo bez měření
DATA_DIR = Path(__file__).resolve().parent.parent / "data"


def build_region(region: str) -> None:
    bbox = REGIONS[region].bbox
    output_path = DATA_DIR / f"candidates_{region}.json"

    print(f"=== Candidate discovery: {region} ===")
    raw = discover_candidates_cached(region, bbox)
    print(f"Raw candidates: {len(raw)}")

    filtered = filter_within_bbox(raw, CZECHIA_BBOX)
    filtered = deduplicate(filtered)
    by_type = {}
    for c in filtered:
        by_type[c.candidate_type] = by_type.get(c.candidate_type, 0) + 1
    print(f"After bbox filter + dedup: {len(filtered)} (by type: {by_type})")

    # Vzorek pro terrain analýzu: MIX typů (zadání Fáze 9 bod 11/12 -
    # "vlastní vizuální jazyk podle typu" potřebuje na ukázku obojí).
    # Vyhlídky/rozhledny v OSM skoro nikdy nemají tag nadmořské výšky,
    # takže řazení čistě podle elevace by je z top-N úplně vyřadilo
    # (ověřeno - první verze datasetu měla 12/12 vrcholů, 0 vyhlídek).
    # Vezmeme nejvyšší vrcholy, pár vyhlídek a rozhledny (pokud existují)
    # ZVLÁŠŤ, ne jedno společné řazení. Rozhledny jsou v OSM řídké (jen
    # pár na region), takže bereme VŠECHNY dostupné do stropu, aby se
    # nový typ v datasetu vůbec objevil.
    peaks = sorted([c for c in filtered if c.candidate_type == "peak"], key=lambda c: c.elevation_m or 0, reverse=True)
    towers = [c for c in filtered if c.candidate_type == "tower"]
    n_towers = min(3, len(towers))
    # Preferuj vyhlídky se SKUTEČNÝM OSM jménem (ne vygenerovaný fallback
    # "Vyhlídka X°N Y°E") - hezčí ukázka, žádný vliv na to, co se skutečně analyzuje.
    viewpoints = [c for c in filtered if c.candidate_type == "viewpoint" and c.metadata.get("osm_tags", {}).get("name")]
    n_viewpoints = min(4, len(viewpoints))
    n_peaks = SAMPLE_SIZE - n_towers - n_viewpoints
    sample = peaks[:n_peaks] + viewpoints[:n_viewpoints] + towers[:n_towers]
    print(f"\nSample for terrain analysis ({len(sample)} candidates, {min(n_peaks, len(peaks))} peaks + {n_viewpoints} viewpoints + {n_towers} towers):")
    for c in sample:
        print(f"  - {c.name} ({c.candidate_type}, OSM ele={c.elevation_m})")

    when = datetime.now(ZoneInfo("Europe/Prague")).replace(hour=12, minute=0, second=0, microsecond=0)
    print(f"\n=== Terrain analysis (SEQUENTIAL, date={when.date()}) ===")
    ranked_all = []
    timings = []
    for i, candidate in enumerate(sample, start=1):
        t0 = time.time()
        result = analyze_candidates([candidate], when)
        dt = time.time() - t0
        timings.append(dt)
        status = f"rating={result[0].rating.overall_0_100}" if result else "SKIPPED (analysis failed)"
        print(f"  [{i}/{len(sample)}] {candidate.name:30s} {dt:6.1f}s  {status}")
        ranked_all.extend(result)

    ranked = rank_by_rating(ranked_all)
    total_s = sum(timings)
    print(f"\nTotal: {total_s:.1f}s for {len(sample)} candidates (avg {total_s/len(timings):.1f}s/candidate)")
    print(f"Successfully analyzed: {len(ranked)}/{len(sample)}")

    output = {
        "region": region,
        "generated_at": datetime.now(ZoneInfo("Europe/Prague")).isoformat(),
        "analyzed_for_date": when.date().isoformat(),
        "candidate_count": len(ranked),
        "candidates": [
            {
                "id": r.candidate.id,
                "name": r.candidate.name,
                "lat": r.candidate.lat,
                "lon": r.candidate.lon,
                "type": r.candidate.candidate_type,
                "osm_elevation_m": r.candidate.elevation_m,
                "measured_elevation_m": r.analysis.location.elevation_m,
                "rating": r.rating.overall_0_100,
                "rating_label": r.rating.label,
                "sunset_time": r.analysis.solar.astronomical_sunset.isoformat(),
                "sunset_azimuth_deg": r.analysis.solar.sunset_azimuth_deg,
                "sunset_terrain_horizon_deg": r.analysis.solar.sunset_terrain_horizon_deg,
                "delay_minutes": r.analysis.visibility.delay_minutes,
                "terrain_obstruction": r.analysis.visibility.terrain_obstruction,
            }
            for r in ranked
        ],
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(output, indent=2, ensure_ascii=False))
    print(f"\nWritten to {output_path}")
    if ranked:
        print(f"\nBest sunset: {ranked[0].candidate.name} - {ranked[0].rating.overall_0_100}/100 ({ranked[0].rating.label})")


def main() -> None:
    requested = sys.argv[1:] or list(REGIONS)
    unknown = [r for r in requested if r not in REGIONS]
    if unknown:
        raise SystemExit(f"Neznámý region {unknown}. Dostupné: {list(REGIONS)}")
    for region in requested:
        build_region(region)
        print()


if __name__ == "__main__":
    main()
