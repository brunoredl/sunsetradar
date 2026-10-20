"""Milestone "Complete Product Build" - weather komponenta Sunset Score
(zadání bod 11 - "ne jen more clouds = worse"). Heuristika, ne fyzikální
model - zadání bod 11 explicitně říká "nepřeháněj pseudo-vědeckou
přesnost, pokud to nelze spolehlivě podložit daty, udělej to
jednodušší a transparentní". Každé číslo níž je proto zdůvodněné
fyzikální intuicí (jaký typ oblačnosti dělá co), ne kalibrované na
datasetu - to je v komentářích řečeno na rovinu.

Rozlišuje (zadání bod 11):
- NÍZKÁ oblačnost (`cloud_cover_low`) - přímo zakrývá horizont, kam
  Slunce zapadá -> nejvíc škodí, váha 50 %.
- STŘEDNÍ/VYSOKÁ oblačnost (`cloud_cover_mid`/`cloud_cover_high`) -
  Slunce je pod nimi/za nimi, ale ONY jsou to, co se "rozsvítí" barvou
  při západu -> střední množství (~30-55 %) je LEPŠÍ než čisté nebe,
  extrémy (0 % nebo >90 %) jsou horší -> váha 35 %, "zvonová" křivka.
- SRÁŽKY (pravděpodobnost/množství) -> tvrdý penalty, váha 15 %.

Atmosphere (zadání bod 9 - samostatná 10% komponenta finálního Sunset
Score, ne součást weather_score) se počítá TADY (`compute_atmosphere_score()`),
protože zadání bod 30 nevyžaduje samostatný `atmosphere_score.py` soubor -
jen z vlhkosti (humidity) jako hrubá proxy zákalu/haze, žádná přesnější
veličina (aerosoly/AOD) není z Open-Meteo free tier dostupná."""

from __future__ import annotations

from dataclasses import dataclass

from app.weather_provider import WeatherInfo


@dataclass
class WeatherScoreResult:
    score_0_100: float | None  # None = unavailable (žádné počasí)
    detail: str


def _clamp(x: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, x))


def compute_weather_score(weather: WeatherInfo | None) -> WeatherScoreResult:
    if weather is None:
        return WeatherScoreResult(score_0_100=None, detail="Weather data unavailable.")

    low = weather.cloud_cover_low_pct
    mid_high = (weather.cloud_cover_mid_pct + weather.cloud_cover_high_pct) / 2.0

    # Nízká oblačnost přímo blokuje výhled na horizont - lineární penalty,
    # nad ~70% low cloud počítáme prakticky jako "zataženo od země".
    low_cloud_score = _clamp(100.0 * (1.0 - low / 70.0))

    # "Zvonová" křivka pro střední/vysokou oblačnost - vrchol kolem 40%
    # (nejvíc prostoru pro nasvícení mraků barvou), klesá k oběma
    # extrémům (0% = jen čisté nebe bez dramatu, 100% = plně zatažené
    # bez proražení světla).
    if mid_high <= 40:
        drama_score = 45.0 + (mid_high / 40.0) * 55.0  # 45 (jasno) -> 100 (40%)
    else:
        drama_score = 100.0 - ((mid_high - 40.0) / 60.0) * 65.0  # 100 -> 35 (souvislá vysoká oblačnost)
    drama_score = _clamp(drama_score)

    precip_prob = weather.precipitation_probability_pct if weather.precipitation_probability_pct is not None else 0.0
    precip_score = _clamp(100.0 - precip_prob)
    if weather.precipitation_mm > 0.1:
        precip_score = _clamp(precip_score - 30.0)  # skutečně prší/sněží teď, ne jen pravděpodobnost

    overall = 0.50 * low_cloud_score + 0.35 * drama_score + 0.15 * precip_score
    overall = round(_clamp(overall), 1)

    bits = [f"{low:.0f}% low cloud", f"{mid_high:.0f}% mid/high cloud"]
    if precip_prob > 20:
        bits.append(f"{precip_prob:.0f}% chance of precipitation")
    return WeatherScoreResult(score_0_100=overall, detail=", ".join(bits))


@dataclass
class AtmosphereScoreResult:
    score_0_100: float | None
    detail: str


def compute_atmosphere_score(weather: WeatherInfo | None) -> AtmosphereScoreResult:
    """Hrubá proxy "atmosférických podmínek pro barvu" z vlhkosti -
    zadání bod 9 (samostatná 10% komponenta). Střední vlhkost (~40-65%)
    bereme jako neutrálně dobrou, velmi vysoká (>85%, blízko mlze/opar)
    jako zhoršující kontrast a barvy, velmi nízká (<20%, velmi suchý
    vzduch) jako neutrální až mírně horší (méně rozptylu světla).
    ÚMYSLNĚ jednoduché - Open-Meteo free tier nemá aerosoly/AOD, jen
    tohle je poctivé přiznat (zadání bod 11 - "nepřeháněj přesnost")."""
    if weather is None:
        return AtmosphereScoreResult(score_0_100=None, detail="Atmospheric data unavailable.")

    h = weather.humidity_pct
    if h <= 65:
        score = 70.0 + (h / 65.0) * 30.0 if h >= 40 else 55.0 + (h / 40.0) * 15.0
    else:
        score = 100.0 - ((h - 65.0) / 35.0) * 55.0
    score = round(_clamp(score), 1)
    return AtmosphereScoreResult(score_0_100=score, detail=f"{h:.0f}% humidity")
