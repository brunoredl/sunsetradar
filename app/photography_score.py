"""Milestone "Complete Product Build" - photography potential komponenta
(zadání bod 9/30). NENÍ nová nezávislá měřená veličina - je to
TRANSPARENTNÍ KOMPOZIT už spočítaných komponent (terrain openness,
weather "drama", pomalý západ), vážený podle toho, co fotografy
typicky zajímá: otevřený horizont (musí vidět slunce), zajímavá
oblačnost (barvy), a delší golden hour (pomalý sestup). Zadání bod 9
to výslovně dovoluje - "photography potential" je inherentně
odvozená/kombinovaná vlastnost, ne raw data."""

from __future__ import annotations

from dataclasses import dataclass

WEIGHT_TERRAIN_OPENNESS = 0.40
WEIGHT_WEATHER_DRAMA = 0.35
WEIGHT_SUN_POSITION = 0.25
assert abs(WEIGHT_TERRAIN_OPENNESS + WEIGHT_WEATHER_DRAMA + WEIGHT_SUN_POSITION - 1.0) < 1e-9


@dataclass
class PhotographyScoreResult:
    score_0_100: float | None
    detail: str


def compute_photography_score(
    terrain_score_0_100: float,
    weather_score_0_100: float | None,
    sun_position_score_0_100: float,
) -> PhotographyScoreResult:
    """`terrain_score_0_100`/`sun_position_score_0_100` jsou vždy
    dostupné (žádná síť); pokud weather chybí, komponenta se PŘEROZDĚLÍ
    mezi zbylé dvě (stejný princip jako u `sunset_score.py` - zadání
    bod 32, "nikdy unknown -> fake default")."""
    if weather_score_0_100 is None:
        w_terrain = WEIGHT_TERRAIN_OPENNESS / (WEIGHT_TERRAIN_OPENNESS + WEIGHT_SUN_POSITION)
        w_sun = WEIGHT_SUN_POSITION / (WEIGHT_TERRAIN_OPENNESS + WEIGHT_SUN_POSITION)
        score = w_terrain * terrain_score_0_100 + w_sun * sun_position_score_0_100
        detail = "based on terrain and sun angle only (no weather data)"
    else:
        score = (
            WEIGHT_TERRAIN_OPENNESS * terrain_score_0_100
            + WEIGHT_WEATHER_DRAMA * weather_score_0_100
            + WEIGHT_SUN_POSITION * sun_position_score_0_100
        )
        detail = "based on terrain, cloud potential and sun angle"
    return PhotographyScoreResult(score_0_100=round(score, 1), detail=detail)
