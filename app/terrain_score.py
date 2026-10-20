"""Milestone "Complete Product Build" - terrain komponenta finálního
Sunset Score (zadání bod 30 - "odděl terrain_score.py"). ŽÁDNÁ nová
matematika - `compute_sunset_rating()` (app/sunset_rating.py, Fáze 6.6,
zamčený/testovaný vzorec) UŽ JE přesně "obstruction at actual sunset
azimuth + openness around sunset azimuth + visible duration + delay"
(zadání bod 10), jen se předtím nikam neskládala s počasím. Tenhle
modul ji pouze zabalí jako jednu komponentu nové `SunsetScore`."""

from __future__ import annotations

from dataclasses import dataclass

from app.analyze_location import LocationAnalysis
from app.sunset_rating import SunsetRating, compute_sunset_rating


@dataclass
class TerrainScoreResult:
    score_0_100: float
    rating: SunsetRating  # celý původní objekt - UI ho dál používá beze změny (stat řádky, Why this place, panorama)


def compute_terrain_score(analysis: LocationAnalysis) -> TerrainScoreResult:
    """Terrain je VŽDY dostupný, pokud se sem vůbec dostaneme (žádná
    síť navíc, jen `analyze_location()`, který už proběhl) - na rozdíl
    od weather/visibility nemůže být `unavailable`."""
    rating = compute_sunset_rating(analysis)
    return TerrainScoreResult(score_0_100=float(rating.overall_0_100), rating=rating)
