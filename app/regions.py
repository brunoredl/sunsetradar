"""Milestone "Real Candidate Catalog + Spatial Search" - jediný zdroj
pravdy pro registrované regiony (bbox + název). Dřív byl tenhle
seznam duplikovaný ve dvou souborech (app/main.py A
scripts/build_candidate_dataset.py) - to je přesně to, co audit
označil jako je třeba sjednotit. Přidání regionu je teď JEDEN řádek
tady, nikde jinde."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Region:
    key: str
    name: str
    bbox: tuple[float, float, float, float]  # south, west, north, east


REGIONS: dict[str, Region] = {
    "beskydy": Region("beskydy", "Beskydy", (49.4, 18.3, 49.7, 18.7)),
    "krkonose": Region("krkonose", "Krkonoše", (50.55, 15.3, 50.85, 15.9)),
    "vsetinsko": Region("vsetinsko", "Vsetínsko", (49.15, 17.5, 49.6, 18.3)),
}
