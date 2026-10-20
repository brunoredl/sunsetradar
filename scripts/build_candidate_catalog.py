"""Milestone "Real Candidate Catalog + Spatial Search" - offline
skript: sloučí syrová Overpass data ze VŠECH registrovaných regionů
(app/regions.py) do jednoho centrálního `data/candidate_catalog.json`.

Spouští se RUČNĚ (`python scripts/build_candidate_catalog.py`), stejně
jako scripts/build_candidate_dataset.py - žádná terrain analýza tady
neprobíhá (to dělá `/api/find_sunset` naživo nad malou prefiltrovanou
podmnožinou katalogu, viz app/main.py), takže tenhle skript je RYCHLÝ
(jen síťové/cache čtení + deduplikace, žádný DEM)."""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.candidate_catalog import DEFAULT_CATALOG_PATH, build_catalog


def main() -> None:
    t0 = time.time()
    catalog = build_catalog()
    catalog["generated_at"] = datetime.now(ZoneInfo("Europe/Prague")).isoformat()
    dt = time.time() - t0

    by_region: dict[str, int] = {}
    by_type: dict[str, int] = {}
    for c in catalog["candidates"]:
        by_region[c["region"]] = by_region.get(c["region"], 0) + 1
        by_type[c["candidate_type"]] = by_type.get(c["candidate_type"], 0) + 1

    print(f"Raw candidates per region (before global dedup): {catalog['raw_count_per_region']}")
    print(f"Total after bbox filter + GLOBAL dedup: {catalog['candidate_count']}")
    print(f"By region: {by_region}")
    print(f"By type: {by_type}")
    print(f"Build time: {dt:.2f}s")

    DEFAULT_CATALOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    DEFAULT_CATALOG_PATH.write_text(json.dumps(catalog, indent=2, ensure_ascii=False))
    print(f"\nWritten to {DEFAULT_CATALOG_PATH}")


if __name__ == "__main__":
    main()
