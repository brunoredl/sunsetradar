"""Milestone "Complete Product Build" - uživatelské preference a
"user match" (zadání bod 13/14/15). ODDĚLENO od fyzikálního Sunset
Score (zadání bod 15 - "preference nesmí tajně měnit fyzikální
hodnocení") - `user_match_pct` je samostatné číslo vedle
`overall_0_100`, nikdy do něj nezasahuje.

POCTIVOST O DATECH (zadání bod 14/32): nemáme routing/trail/elevation-gain
dataset pro kandidáty - jen distance/elevation/candidate_type z
katalogu. Proto je "Hiking effort" ODHAD z distance_km, VŽDY označený
jako odhad, NE "2.3 km hike" jistota, kterou nemáme.

Scény preference "Water"/"City"/"Forest" MAJÍ (od milestone "Complete
Product Build", pokračování - `app/environment_context.py`) skutečný
datový signál: `near_water`/`near_forest`/`near_city`, počítaný jednou
při stavbě katalogu z reálných OSM dat (Overpass), ne fake labels.
Všech pět scény preferencí (Mountain/Water/City/Forest) i
Photography/Dramatic/Clear/Hiking effort proto dnes mají skutečný
datový protějšek - žádná preference už není `_UNSUPPORTED_SCENE_TYPES`."""

from __future__ import annotations

from dataclasses import dataclass, field

# Zadání bod 14 - čistě z distance (jediné, co skutečně máme) -
# meze zvolené jako rozumný, ne kalibrovaný odhad "kolik typicky
# znamená který stupeň námahy pro pěší přístup k vyhlídce v ČR".
ACCESS_EASY_MAX_KM = 3.0
ACCESS_MODERATE_MAX_KM = 10.0
ACCESS_HIKE_MAX_KM = 25.0
# nad ACCESS_HIKE_MAX_KM = "adventure"

_ACCESS_LEVELS = ("easy", "moderate", "hike", "adventure")

# BUG NALEZENÝ (živé použití - Lysá hora, 1324 m, nejvyšší vrchol Beskyd,
# vyšla jako "easy access", protože hledaný střed byl vzdušnou čarou jen
# 2 km od vrcholu): vzdušná vzdálenost neříká NIC o převýšení, které je
# potřeba vystoupat - krátká vzdušná vzdálenost k vysokému vrcholu
# prakticky vždy znamená strmý výstup, ne "easy". Elevace je DRUHÝ
# signál, co skutečně máme (z katalogu) - funguje jako DOLNÍ HRANICE
# (floor), nikdy distanci nepřepisuje směrem dolů, jen nahoru, pokud
# elevace naznačuje, že "easy"/"moderate" by bylo zavádějící. Pořád jen
# heuristika (ne přesná trasa/převýšení), ale poctivější než čistá
# vzdušná vzdálenost.
ELEVATION_HIKE_FLOOR_M = 1200.0
ELEVATION_MODERATE_FLOOR_M = 800.0

# Scény preference, které DNES máme reálný datový signál pro vyhodnocení
# (mountain z candidate_type/elevation, water/forest/city z
# app/environment_context.py přes candidate katalog).
_SUPPORTED_SCENE_TYPES = {"mountain", "water", "city", "forest"}


@dataclass
class UserPreferences:
    scene_types: list[str] = field(default_factory=list)  # "mountain" | "water" | "city" | "forest"
    photography: bool = False
    dramatic: bool = False
    clear: bool = False
    hiking_effort: str | None = None  # "easy" | "moderate" | "hike" | "adventure"
    # Milník 12 (zadání bod 4) - čtyři nové preference, každá s vlastním
    # SKUTEČNÝM datovým protějškem (ne jen UI navíc): `inversion`/
    # `vivid_colors` čtou nové `classify_inversion_potential()`/
    # `classify_color_potential()` (app/sunset_score.py), `sun_on_horizon`/
    # `panorama` znovupoužívají UŽ existující terénní komponenty
    # konkrétního bodu (`rating.components.horizon_at_azimuth`/
    # `.sector_openness`, app/sunset_rating.py) - žádná nová terénní
    # metrika, jen nové VYUŽITÍ už spočítaného čísla pro match.
    inversion: bool = False
    vivid_colors: bool = False
    sun_on_horizon: bool = False
    panorama: bool = False

    @property
    def is_empty(self) -> bool:
        return not (
            self.scene_types or self.photography or self.dramatic or self.clear or self.hiking_effort
            or self.inversion or self.vivid_colors or self.sun_on_horizon or self.panorama
        )


def estimate_access_level(distance_km: float, elevation_m: float | None = None) -> str:
    """ODHAD ze vzdálenosti + (pokud známá) elevace (zadání bod 14 -
    "nepředstírej přesnost, pokud nemáme trail/routing data"). Blízko !=
    vždy snadné (může to být příkrá stezka), ale je to nejlepší reálný
    signál, co dnes máme - elevace jako floor opravuje nejhorší případ
    (vysoký vrchol blízko hledaného středu vzdušnou čarou)."""
    if distance_km <= ACCESS_EASY_MAX_KM:
        level = "easy"
    elif distance_km <= ACCESS_MODERATE_MAX_KM:
        level = "moderate"
    elif distance_km <= ACCESS_HIKE_MAX_KM:
        level = "hike"
    else:
        level = "adventure"

    if elevation_m is not None:
        floor = None
        if elevation_m >= ELEVATION_HIKE_FLOOR_M:
            floor = "hike"
        elif elevation_m >= ELEVATION_MODERATE_FLOOR_M:
            floor = "moderate"
        if floor is not None and _ACCESS_LEVELS.index(floor) > _ACCESS_LEVELS.index(level):
            level = floor

    return level


@dataclass
class UserMatchResult:
    match_pct: int | None  # None = žádná preference zadaná (zadání bod 15 - ukázat rozdíl "no preference" vs. "0% match")
    reasons: list[str]
    unavailable_preferences: list[str]  # preference, které jsme NEMOHLI reálně vyhodnotit (zadání bod 14 poctivost)


def compute_user_match(
    prefs: UserPreferences,
    candidate_type: str,
    elevation_m: float | None,
    distance_km: float,
    sunset_type_key: str | None,
    photography_score_0_100: float | None,
    near_water: bool | None = None,
    near_forest: bool | None = None,
    near_city: bool | None = None,
    color_potential_key: str | None = None,
    inversion_key: str | None = None,
    horizon_at_azimuth_0_100: float | None = None,
    sector_openness_0_100: float | None = None,
) -> UserMatchResult:
    """`near_water`/`near_forest`/`near_city` - reálný signál z
    `app/environment_context.py` (přes candidate katalog), `None` když
    candidate nemá tuhle metadatu vůbec (starší katalog bez rebuild) -
    v tom případě je preference poctivě `unavailable`, ne fake match.

    `color_potential_key`/`inversion_key` - z `classify_color_potential()`/
    `classify_inversion_potential()` (app/sunset_score.py), `None` když
    počasí nebylo dostupné. `horizon_at_azimuth_0_100`/`sector_openness_0_100`
    - UŽ existující terénní komponenty KONKRÉTNÍHO bodu
    (`rating.components`, app/sunset_rating.py), vždy dostupné (terén se
    nikdy "nenačítá ze sítě"), takže tyhle dvě preference nikdy nejsou
    `unavailable`."""
    if prefs.is_empty:
        return UserMatchResult(match_pct=None, reasons=[], unavailable_preferences=[])

    signals: list[float] = []  # 0-100 dílčí shody, průměrují se
    reasons: list[str] = []
    unavailable: list[str] = []

    _SCENE_SIGNAL = {"water": near_water, "forest": near_forest, "city": near_city}
    _SCENE_LABEL = {
        "water": "Matches your water view preference.",
        "forest": "Matches your forest surroundings preference.",
        "city": "Matches your cityscape preference.",
    }

    for scene in prefs.scene_types:
        if scene == "mountain":
            is_mountain = candidate_type in ("peak", "hill") and (elevation_m or 0) >= 700
            signals.append(100.0 if is_mountain else 30.0)
            if is_mountain:
                reasons.append("Matches your mountain panorama preference.")
        elif scene in _SCENE_SIGNAL:
            near = _SCENE_SIGNAL[scene]
            if near is None:
                unavailable.append(scene)
                continue
            # Zpětná vazba (živé použití, Brno/Špilberk) - "to vůbec
            # není přírodní, když je to uprostřed Brna". `near_forest`/
            # `near_water` samy o sobě neumí rozlišit "skutečně
            # přírodní místo s kouskem lesa/vody hned vedle" od "hustě
            # městské místo, co náhodou má v okruhu 1.5/2 km taky malý
            # park/řeku" - obojí dostane `near_forest=True`/
            # `near_water=True` stejně (viz NEAR_FOREST_KM/NEAR_WATER_KM,
            # app/environment_context.py). `near_city` (reálný signál ze
            # STEJNÉHO zdroje) dominantní městský charakter správně
            # pozná - pokud je zároveň pravda, "les"/"voda" tam jsou jen
            # vedlejší, ne definující - nepočítá se to jako shoda s
            # "Natural"/"Water view" preferencí. "city" preference touhle
            # podmínkou NENÍ ovlivněná (ta má near_city dávat jako
            # pozitivní signál vždy).
            effectively_near = bool(near) and not (scene in ("water", "forest") and near_city)
            signals.append(100.0 if effectively_near else 25.0)
            if effectively_near:
                reasons.append(_SCENE_LABEL[scene])
        else:
            unavailable.append(scene)

    if prefs.photography:
        if photography_score_0_100 is not None:
            signals.append(photography_score_0_100)
            if photography_score_0_100 >= 75:
                reasons.append("Strong photography potential today.")
        else:
            unavailable.append("photography")

    if prefs.dramatic:
        if sunset_type_key is not None:
            match = 100.0 if sunset_type_key in ("dramatic", "cloudscape") else 40.0
            signals.append(match)
            if match >= 100.0:
                reasons.append("Conditions favor a dramatic sunset, as requested.")
        else:
            unavailable.append("dramatic")

    if prefs.clear:
        if sunset_type_key is not None:
            match = 100.0 if sunset_type_key == "clear" else 40.0
            signals.append(match)
            if match >= 100.0:
                reasons.append("Clear-sky conditions, as requested.")
        else:
            unavailable.append("clear")

    if prefs.hiking_effort:
        access = estimate_access_level(distance_km, elevation_m)
        idx_pref = _ACCESS_LEVELS.index(prefs.hiking_effort) if prefs.hiking_effort in _ACCESS_LEVELS else None
        idx_actual = _ACCESS_LEVELS.index(access)
        if idx_pref is not None:
            diff = abs(idx_pref - idx_actual)
            match = {0: 100.0, 1: 60.0}.get(diff, 25.0)
            signals.append(match)
            if diff == 0:
                reasons.append(f"Estimated access effort ({access}, based on distance) matches what you're looking for.")

    # Milník 12 (zadání bod 4) - čtyři nové preference. `vivid_colors`/
    # `inversion` jsou kategorické (stejný binarizační vzor jako
    # `dramatic`/`clear` výše - 100, pokud klíč sedí, jinak nízká
    # částečná shoda, `unavailable` když počasí chybí). `sun_on_horizon`/
    # `panorama` použijí SKUTEČNÉ 0-100 terénní skóre přímo jako signál
    # (stejný vzor jako `photography_score_0_100` výše) - žádné nové
    # binární prahování navíc, jen průhledné znovupoužití existujícího
    # terénního čísla.
    if prefs.vivid_colors:
        if color_potential_key is not None:
            match = 100.0 if color_potential_key == "vivid" else (55.0 if color_potential_key == "warm" else 20.0)
            signals.append(match)
            if color_potential_key == "vivid":
                reasons.append("Good potential for a vivid, colorful sky, as requested.")
        else:
            unavailable.append("vivid_colors")

    if prefs.inversion:
        if inversion_key is not None:
            match = {"favorable": 100.0, "possible": 55.0, "uncertain": 35.0, "unlikely": 15.0}.get(inversion_key, 15.0)
            signals.append(match)
            if inversion_key == "favorable":
                reasons.append("Favorable inversion/sea-of-clouds potential, as requested.")
        else:
            unavailable.append("inversion")

    if prefs.sun_on_horizon:
        if horizon_at_azimuth_0_100 is not None:
            signals.append(horizon_at_azimuth_0_100)
            if horizon_at_azimuth_0_100 >= 75.0:
                reasons.append("Open horizon right where the sun sets, as requested.")
        else:
            unavailable.append("sun_on_horizon")

    if prefs.panorama:
        if sector_openness_0_100 is not None:
            signals.append(sector_openness_0_100)
            if sector_openness_0_100 >= 75.0:
                reasons.append("Wide, open panorama around the sunset direction, as requested.")
        else:
            unavailable.append("panorama")

    if not signals:
        return UserMatchResult(match_pct=None, reasons=[], unavailable_preferences=unavailable)

    match_pct = round(sum(signals) / len(signals))
    return UserMatchResult(match_pct=match_pct, reasons=reasons, unavailable_preferences=unavailable)
