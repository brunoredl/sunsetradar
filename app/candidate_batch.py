"""Fáze 8 - napojení `Candidate` (app/candidate_discovery.py) na
existující `analyze_location()`/`compute_sunset_rating()` (zadání bod
13). ŽÁDNÝ nový terrain/solar výpočet - jen orchestrace nad hotovými
funkcemi, stejně jako `app/analyze_location.py` samo je orchestrace nad
nižšími komponentami.

DŮLEŽITÉ (zadání bod 14 - "neudělej 50 kandidátů -> 50 současných DEM
requestů"): `analyze_candidates()` zpracovává kandidáty VÝHRADNĚ
SEKVENČNĚ (obyčejný for-cyklus, žádný threading/asyncio.gather). Reálně
naměřené časy (viz PLAN.md Fáze 8 audit + `scripts/build_candidate_dataset.py`
výstup) ukazují, že jeden `analyze_location()` běh trvá řádově sekundy
až desítky sekund - souběžné spouštění desítek takových výpočtů by
zahltilo CPU (GIL) i externí DEM zdroje. Z toho důvodu tenhle modul
NENÍ volaný přímo z HTTP request handleru pro živý dotaz - používá ho
offline dávkový skript, výsledek se cachuje na disk a webová vrstva ho
jen čte (`GET /api/candidates`, viz app/main.py)."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from app.analyze_location import LocationAnalysis, analyze_location
from app.candidate_discovery import Candidate
from app.sunset_rating import SunsetRating, compute_sunset_rating

logger = logging.getLogger(__name__)


@dataclass
class RankedCandidate:
    candidate: Candidate
    analysis: LocationAnalysis
    rating: SunsetRating


def analyze_candidates(candidates: list[Candidate], when: datetime) -> list[RankedCandidate]:
    """SEKVENČNĚ spočítá terrain analýzu + rating pro každého kandidáta.
    Kandidát, u kterého `analyze_location()` selže (např. NODATA na
    okraji DEM pokrytí), se PŘESKOČÍ - volající dostane jen ty, co se
    skutečně podařilo spočítat (žádný fake/prázdný výsledek, zadání
    bod 19 "nevymýšlej data")."""
    results: list[RankedCandidate] = []
    for candidate in candidates:
        try:
            analysis = analyze_location(candidate.lat, candidate.lon, when)
        except Exception as exc:  # noqa: BLE001 - záměrně široké: jeden špatný kandidát nesmí shodit celou dávku
            logger.warning("Terrain analýza selhala pro kandidáta %s (%s): %s", candidate.id, candidate.name, exc)
            continue
        rating = compute_sunset_rating(analysis)
        results.append(RankedCandidate(candidate=candidate, analysis=analysis, rating=rating))
    return results


def rank_by_rating(ranked: list[RankedCandidate]) -> list[RankedCandidate]:
    """Deterministické řazení sestupně podle `rating.overall_0_100`
    (zadání bod 15 - "seřadit podle ratingu"). Při shodě ratingu
    zachovává stabilní pořadí podle `stable sort` Pythonu (vstupní
    pořadí), takže výsledek je reprodukovatelný pro stejný vstup."""
    return sorted(ranked, key=lambda r: r.rating.overall_0_100, reverse=True)
