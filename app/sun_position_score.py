"""Milestone "Complete Product Build" - sun position komponenta (zadání
bod 9/30). Reálná, počítatelná fyzikální veličina odlišná od terrain
(ta hodnotí PŘEKÁŽKY, tahle hodnotí GEOMETRII SLUNCE samotnou): rychlost
klesání Slunce k horizontu těsně před západem (°/min), odvozená ze
STÁVAJÍCÍHO `app.solar.sun_position()` - žádný nový výpočetní model.

Fyzikální zdůvodnění: čím POMALEJI Slunce klesá k horizontu, tím delší
je "golden hour"/barevné světlo těsně před západem (fotograficky
hodnotnější) - rychlost klesání závisí na zeměpisné šířce a ročním
období (u pozorovatele v ČR nejpomalejší kolem letního slunovratu,
nejrychlejší kolem rovnodenností), NE na terénu ani počasí."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from astral import Observer

from app.solar import sun_position

# Empiricky rozumné meze pro zeměpisné šířky ČR (49-51°N) - rychlejší
# klesání (rovnodennost) až pomalejší (letní slunovrat). Nejde o
# kalibrovaný dataset, jen fyzikálně podloženy odhad rozsahu (zadání
# bod 11 - "nepřeháněj přesnost, buď transparentní").
FAST_DESCENT_DEG_PER_MIN = 0.32
SLOW_DESCENT_DEG_PER_MIN = 0.14


@dataclass
class SunPositionScoreResult:
    score_0_100: float
    descent_rate_deg_per_min: float
    detail: str


def compute_sun_position_score(observer: Observer, sunset_time: datetime) -> SunPositionScoreResult:
    """Vždy dostupné (žádná síť) - stejná vstupní data jako terrain."""
    before = sun_position(observer, sunset_time - timedelta(minutes=2))
    after = sun_position(observer, sunset_time + timedelta(minutes=2))
    descent_rate = (before.altitude_deg - after.altitude_deg) / 4.0  # °/min

    span = FAST_DESCENT_DEG_PER_MIN - SLOW_DESCENT_DEG_PER_MIN
    fraction_slow = max(0.0, min(1.0, (FAST_DESCENT_DEG_PER_MIN - descent_rate) / span))
    score = round(fraction_slow * 100.0, 1)

    return SunPositionScoreResult(
        score_0_100=score,
        descent_rate_deg_per_min=round(descent_rate, 3),
        detail=f"sun descending at {descent_rate:.2f}°/min near sunset",
    )
