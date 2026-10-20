"""Milestone "Complete Product Build" - visibility komponenta (zadání
bod 9/30). Založeno na `WeatherInfo.visibility_m` (Open-Meteo skutečná
meteorologická dohlednost, ne terénní horizont - to je terrain_score.py).
Normalizace: 0-40 km typický rozsah reálně pozorované dohlednosti v ČR
(velmi znečištěný/mlhavý vzduch ~1-5 km, čirý horský den 30-40+ km) -
nad 40 km už další zisk prakticky nerozeznáme okem, strop na 100."""

from __future__ import annotations

from dataclasses import dataclass

from app.weather_provider import WeatherInfo

MAX_MEANINGFUL_VISIBILITY_M = 40_000.0


@dataclass
class VisibilityScoreResult:
    score_0_100: float | None
    detail: str


def compute_visibility_score(weather: WeatherInfo | None) -> VisibilityScoreResult:
    if weather is None or weather.visibility_m is None:
        return VisibilityScoreResult(score_0_100=None, detail="Visibility data unavailable.")

    vis_km = weather.visibility_m / 1000.0
    score = round(max(0.0, min(1.0, weather.visibility_m / MAX_MEANINGFUL_VISIBILITY_M)) * 100.0, 1)
    return VisibilityScoreResult(score_0_100=score, detail=f"{vis_km:.0f} km visibility")
