"""Milestone "Complete Product Build" - finální transparentní Sunset
Score 0-100 (zadání bod 8/9/30). Skládá terrain (existující, beze
změny) + weather + visibility + atmosphere + sun position + photography
komponenty s VAHAMI ze zadání (Weather 30 %, Terrain 25 %, Visibility
15 %, Atmosphere 10 %, Sun position 10 %, Photography 10 %).

DETERMINISTICKÉ, REPRODUKOVATELNÉ, VYSVĚTLITELNÉ (zadání bod 8) - ŽÁDNÉ
AI ve výpočtu. Když komponenta chybí (typicky weather - síť/timeout),
její váha se PŘEROZDĚLÍ mezi dostupné komponenty (ne fake default 50,
ne tichá nula - zadání bod 32 "unknown -> nikdy fake default")."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from astral import Observer

from app.analyze_location import LocationAnalysis
from app.photography_score import compute_photography_score
from app.sun_position_score import compute_sun_position_score
from app.sunset_rating import SunsetRating
from app.terrain_score import compute_terrain_score
from app.visibility_score import compute_visibility_score
from app.weather_provider import WeatherInfo
from app.weather_score import compute_atmosphere_score, compute_weather_score

WEIGHTS: dict[str, float] = {
    "weather": 0.30,
    "terrain": 0.25,
    "visibility": 0.15,
    "atmosphere": 0.10,
    "sun_position": 0.10,
    "photography": 0.10,
}
assert abs(sum(WEIGHTS.values()) - 1.0) < 1e-9


@dataclass
class ScoreComponent:
    key: str
    label: str
    value_0_100: float | None  # None = unavailable
    weight: float
    availability: str  # "real" | "derived" | "unavailable" (zadání bod 32)
    detail: str


@dataclass
class SunsetTypeResult:
    key: str | None  # None = "unknown", UI musí badge skrýt, ne zobrazit prázdný
    label: str | None
    confidence_text: str | None  # "Conditions favor..." formulace (zadání bod 12), NIKDY jistota


@dataclass
class SunsetScoreResult:
    overall_0_100: int
    components: list[ScoreComponent]
    sunset_type: SunsetTypeResult
    rating: SunsetRating  # původní terrain-only rating objekt - zachován pro zpětnou kompatibilitu (panorama/Why list dřív)
    weather: WeatherInfo | None


def _reweighted_overall(components: list[ScoreComponent]) -> int:
    available = [c for c in components if c.value_0_100 is not None]
    total_weight = sum(c.weight for c in available)
    if total_weight <= 0:
        return 0  # nemůže nastat v praxi - terrain a sun_position jsou vždy dostupné
    overall = sum(c.value_0_100 * c.weight for c in available) / total_weight
    return round(max(0.0, min(100.0, overall)))


def classify_sunset_type(weather: WeatherInfo | None, elevation_m: float, terrain_score_0_100: float, photography_score_0_100: float | None) -> SunsetTypeResult:
    """Jednoduchá, prioritně seřazená pravidla nad SKUTEČNÝMI hodnotami
    (zadání bod 12) - jeden odznak, ne "deset typů najednou". Bez
    počasí se badge SCHOVÁ (žádné "unknown" zobrazování, zadání bod 32)."""
    if weather is None:
        return SunsetTypeResult(key=None, label=None, confidence_text=None)

    low = weather.cloud_cover_low_pct
    mid_high = (weather.cloud_cover_mid_pct + weather.cloud_cover_high_pct) / 2.0
    total = weather.cloud_cover_pct

    if elevation_m >= 900 and terrain_score_0_100 >= 85:
        return SunsetTypeResult("mountain", "Mountain Sunset", "Strong high-elevation panorama context.")
    if low >= 55 or total >= 90:
        return SunsetTypeResult("cloudscape", "Cloudscape", "Heavy cloud cover may create diffused, moody light.")
    if low < 20 and 20 <= mid_high <= 65:
        return SunsetTypeResult("dramatic", "Dramatic Sunset", "Conditions favor a dramatic, colorful sunset.")
    if total < 15:
        return SunsetTypeResult("clear", "Clear Sunset", "Conditions favor a clean, open sunset.")
    if photography_score_0_100 is not None and photography_score_0_100 >= 85:
        return SunsetTypeResult("photography", "Photography Sunset", "Combination of conditions well suited for photography.")
    if weather.humidity_pct >= 80 and weather.visibility_m is not None and weather.visibility_m < 15000:
        return SunsetTypeResult("atmospheric", "Atmospheric Sunset", "Hazy conditions may create a soft, atmospheric look.")
    return SunsetTypeResult("mixed", "Mixed Conditions", "Conditions are mixed - the exact character is hard to predict.")


@dataclass
class HorizonViewResult:
    key: str | None  # "likely" | "partial" | "unlikely" | None (bez počasí - zadání bod 32, žádný fake default)
    label: str | None
    low_cloud_pct: float | None  # skutečná naměřená hodnota, pro transparentnost/debug


def classify_general_sky(weather: WeatherInfo | None) -> SunsetTypeResult:
    """Milník "Postupné vylepšení" (Fáze 2) - "obecná předpověď oblohy,
    nezávislá na konkrétním vyhlídkovém místě". ÚMYSLNĚ volá STEJNOU
    `classify_sunset_type()` (žádná nová heuristika, žádné riziko
    rozjetí dvou logik, které by se časem rozešly), jen s neutrálními
    terén/fotografie vstupy (0/0/None) - funkce pak nemůže vybrat
    "mountain" (vyžaduje elevation_m >= 900) ani "photography" (vyžaduje
    reálné photography skóre), takže výsledek je čistě z oblačnosti/
    vlhkosti/dohlednosti, ne z terénu KONKRÉTNÍHO bodu."""
    return classify_sunset_type(weather, elevation_m=0.0, terrain_score_0_100=0.0, photography_score_0_100=None)


@dataclass
class ColorPotentialResult:
    key: str | None  # "vivid" | "warm" | "muted" | None (bez počasí, zadání bod 32)
    label: str | None
    detail: str | None  # lidsky čitelné "proč" - stejná prozaická formulace jako SunsetTypeResult.confidence_text


# Milník 12 (zadání bod 2) - "barvy západu - skutečná funkce, ne dekorace".
# ÚMYSLNĚ používá STEJNÁ pásma oblačnosti jako `classify_sunset_type()`
# výše (ne novou, nesourodou heuristiku) - jen přeorganizovaná kolem
# jedné konkrétní otázky ("jak barevná bude obloha"), ne kolem "typu"
# západu. Fotografická realita, ne vymyšlené pravidlo: rozptýlené
# střední/vysoké mraky fungují jako "plátno", které chytá a odráží
# červené/růžové světlo (nejvýraznější barvy) - zcela jasná obloha dá
# čistý, ale míň dramatický oranžový tón - hustá nízká/celková oblačnost
# blokuje přímé světlo úplně (ploché, tlumené barvy). Jasná obloha !=
# nejbarevnější obloha (zadání "úplně jasná obloha nemusí znamenat
# nejvýraznější barvy").
def classify_color_potential(weather: WeatherInfo | None) -> ColorPotentialResult:
    if weather is None:
        return ColorPotentialResult(None, None, None)

    low = weather.cloud_cover_low_pct
    mid_high = (weather.cloud_cover_mid_pct + weather.cloud_cover_high_pct) / 2.0
    total = weather.cloud_cover_pct

    if low >= 55 or total >= 90:
        return ColorPotentialResult(
            "muted", "Muted, diffused light",
            "Heavy cloud cover is likely to block most direct light near the horizon - expect flat, muted tones rather than strong color.",
        )
    if low < 20 and 20 <= mid_high <= 65:
        return ColorPotentialResult(
            "vivid", "Potential for a red-to-pink sky",
            "Scattered mid/high cloud with an open horizon is the classic combination for clouds catching strong red and pink light.",
        )
    if total < 15:
        return ColorPotentialResult(
            "warm", "Likely warm orange tones",
            "A mostly clear sky usually gives a clean, warm-orange sunset, but with little cloud to catch extra color it's unlikely to be dramatic.",
        )
    if low < 35:
        return ColorPotentialResult(
            "warm", "Some color potential",
            "Partial cloud cover may add some color, though the mix is less ideal than a more scattered mid/high layer.",
        )
    return ColorPotentialResult(
        "muted", "Uncertain - mixed cloud signals",
        "Cloud conditions are mixed enough that color potential is hard to call with confidence.",
    )


@dataclass
class InversionPotentialResult:
    key: str | None  # "favorable" | "possible" | "uncertain" | "unlikely" | None
    label: str | None
    detail: str | None
    low_cloud_pct: float | None  # skutečná naměřená hodnota, pro transparentnost
    dew_point_spread_c: float | None  # odhad (viz estimate_dew_point_c), pro transparentnost


def estimate_dew_point_c(temperature_c: float, humidity_pct: float) -> float:
    """Standardní zjednodušená (Lawrence 2005) aproximace rosného bodu z
    teploty a relativní vlhkosti - ne vymyšlený vzorec, běžně používaná
    meteorologická aproximace. Použitá jen jako orientační signál "jak
    blízko je vzduch u svého nasycení" (= náchylnost k mlze/nízké
    oblačnosti), ne jako přesná meteorologická veličina."""
    return temperature_c - ((100.0 - humidity_pct) / 5.0)


# Elevace nad touhle hranicí se v ČR typicky zvedá nad běžnou výšku
# údolní mlhy/nízké inverzní vrstvy (hrubý, NEKALIBROVANÝ odhad - stejná
# poctivost jako u `ELEVATION_HIKE_FLOOR_M` v app/personalization.py:
# "pořád jen heuristika", ne naměřená výška mlhy, kterou z dostupných
# dat nemáme - zadání bod 3 "pokud nedokážeme spolehlivě určit výšku
# horní hranice mlhy, netvrď, že konkrétní vrchol bude určitě nad ní").
INVERSION_ELEVATION_FLOOR_M = 600.0
INVERSION_NEAR_SATURATION_SPREAD_C = 3.0
INVERSION_LOW_CLOUD_THRESHOLD_PCT = 55.0
INVERSION_CLEAR_ABOVE_THRESHOLD_PCT = 35.0


def classify_inversion_potential(weather: WeatherInfo | None, elevation_m: float) -> InversionPotentialResult:
    """Milník 12 (zadání bod 3) - "inverze a mlha". Kombinuje čtyři
    SKUTEČNÉ signály, které máme (nízká oblačnost = mlha/nízký strop,
    střední+vysoká oblačnost = jestli je nad tím jasno, teplotu/vlhkost
    -> odhad rosného bodu = náchylnost k mlze, a nadmořskou výšku bodu
    z katalogu) - ŽÁDNÁ jednotlivá podmínka sama o sobě nerozhoduje
    (zadání "nespoléhej na jedinou teplotní podmínku"). Výsledek je
    VŽDY formulovaný jako potenciál/možnost, nikdy jako potvrzená
    inverze - nemáme vertikální profil atmosféry, jen povrchová data."""
    if weather is None:
        return InversionPotentialResult(None, None, None, None, None)

    low = weather.cloud_cover_low_pct
    mid_high = (weather.cloud_cover_mid_pct + weather.cloud_cover_high_pct) / 2.0
    dew_point_c = estimate_dew_point_c(weather.temperature_c, weather.humidity_pct)
    spread = round(weather.temperature_c - dew_point_c, 1)

    near_saturation = spread <= INVERSION_NEAR_SATURATION_SPREAD_C
    clear_above = mid_high < INVERSION_CLEAR_ABOVE_THRESHOLD_PCT
    low_fog_signal = low >= INVERSION_LOW_CLOUD_THRESHOLD_PCT
    elevated_enough = elevation_m >= INVERSION_ELEVATION_FLOOR_M

    if low_fog_signal and clear_above and elevated_enough and near_saturation:
        return InversionPotentialResult(
            "favorable", "Inversion potential looks favorable",
            "Low cloud/fog signal near the surface combined with clearer skies higher up and a reasonably elevated point - "
            "a classic (but not guaranteed) setup for a sea of clouds. This is a terrain+weather estimate, not a verified "
            "inversion forecast - we don't have the vertical atmosphere profile to confirm the fog-top height.",
            low, spread,
        )
    if low_fog_signal and clear_above:
        return InversionPotentialResult(
            "possible", "Low clouds or fog possible in valleys",
            "Low-level cloud/fog signal with clearer skies above - lower elevations may sit in cloud while higher ground "
            "stays clear. We can't determine the exact fog-top height from this data.",
            low, spread,
        )
    if low_fog_signal:
        return InversionPotentialResult(
            "uncertain", "Uncertain - conditions look mixed",
            "Low clouds are likely, but the sky higher up doesn't look clearly open, so it's unclear whether elevated "
            "ground stays above the cloud layer.",
            low, spread,
        )
    return InversionPotentialResult(
        "unlikely", "Low inversion/fog potential",
        "No strong signal of low-level cloud or fog forming - conditions don't currently point to a sea-of-clouds setup.",
        low, spread,
    )


def classify_general_inversion_potential(weather: WeatherInfo | None) -> InversionPotentialResult:
    """Milník 12 - "obecná" verze `classify_inversion_potential()`, stejný
    princip jako `classify_general_sky()` výše (neutralizuje parametr,
    který by jinak vázal odpověď na JEDEN konkrétní klikem vybraný bod).
    Odpovídá na "kdyby si dnes vybral/a dostatečně vyvýšené místo v
    okolí, vyplatilo by se to?", ne na elevaci právě vybraného bodu (ta
    může být nížinná, i když se obecná otázka ptá na potenciál inverze
    v regionu obecně) - `INVERSION_ELEVATION_FLOOR_M` je STEJNÁ hranice
    jako u specifického výpočtu, jen použitá jako hypotetický
    "dostatečně vysoko" vstup."""
    return classify_inversion_potential(weather, elevation_m=INVERSION_ELEVATION_FLOOR_M)


def classify_general_horizon_view(weather: WeatherInfo | None) -> HorizonViewResult:
    """Milník "Postupné vylepšení" (Fáze 2) - "jestli lze očekávat přímý
    výhled na zapadající slunce", NEZÁVISLE na terénu KONKRÉTNÍHO místa
    (ten se liší bod od bodu - to je `terrain_obstruction`/deterministický
    terénní výpočet jinde). Jediný reálný, obecně platný signál je NÍZKÁ
    oblačnost (`cloud_cover_low_pct`) - ta clonila horizont odkudkoliv v
    oblasti, bez ohledu na konkrétní vyhlídku. Tři jednoduchá, průhledná
    pásma ze skutečné naměřené hodnoty - ŽÁDNÉ nové skóre/vzorec,
    ŽÁDNÉ vymyšlené procento."""
    if weather is None:
        return HorizonViewResult(None, None, None)
    low = weather.cloud_cover_low_pct
    if low < 30.0:
        key, label = "likely", "Sun likely visible at the horizon"
    elif low < 65.0:
        key, label = "partial", "Sun may be partly screened by low clouds"
    else:
        key, label = "unlikely", "Low clouds likely to block the horizon"
    return HorizonViewResult(key, label, low)


def compute_full_sunset_score(
    analysis: LocationAnalysis,
    weather: WeatherInfo | None,
    observer: Observer,
    sunset_time: datetime,
    elevation_m: float,
) -> SunsetScoreResult:
    terrain = compute_terrain_score(analysis)
    weather_result = compute_weather_score(weather)
    atmosphere_result = compute_atmosphere_score(weather)
    visibility_result = compute_visibility_score(weather)
    sun_pos_result = compute_sun_position_score(observer, sunset_time)
    photo_result = compute_photography_score(terrain.score_0_100, weather_result.score_0_100, sun_pos_result.score_0_100)

    components = [
        ScoreComponent("weather", "Weather / Clouds", weather_result.score_0_100, WEIGHTS["weather"],
                       "real" if weather_result.score_0_100 is not None else "unavailable", weather_result.detail),
        ScoreComponent("terrain", "Terrain / Horizon", terrain.score_0_100, WEIGHTS["terrain"], "real", terrain.rating.label),
        ScoreComponent("visibility", "Visibility", visibility_result.score_0_100, WEIGHTS["visibility"],
                       "real" if visibility_result.score_0_100 is not None else "unavailable", visibility_result.detail),
        ScoreComponent("atmosphere", "Atmosphere", atmosphere_result.score_0_100, WEIGHTS["atmosphere"],
                       "derived" if atmosphere_result.score_0_100 is not None else "unavailable", atmosphere_result.detail),
        ScoreComponent("sun_position", "Sun Position", sun_pos_result.score_0_100, WEIGHTS["sun_position"], "real", sun_pos_result.detail),
        ScoreComponent("photography", "Photography Potential", photo_result.score_0_100, WEIGHTS["photography"],
                       "derived" if photo_result.score_0_100 is not None else "unavailable", photo_result.detail),
    ]

    overall = _reweighted_overall(components)
    sunset_type = classify_sunset_type(weather, elevation_m, terrain.score_0_100, photo_result.score_0_100)

    return SunsetScoreResult(
        overall_0_100=overall, components=components, sunset_type=sunset_type, rating=terrain.rating, weather=weather
    )
