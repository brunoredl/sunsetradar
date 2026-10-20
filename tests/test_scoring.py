"""Testy pro nový Sunset Score engine (milestone "Complete Product
Build", zadání bod 31) - terrain_score/weather_score/visibility_score/
sun_position_score/photography_score/sunset_score/personalization.
Scénáře přesně podle zadání: perfect terrain + good weather, blocked
horizon + perfect weather, excellent terrain + heavy rain, excellent
visibility + low clouds, high clouds + clear horizon, no weather data,
user preferences (hiking/easy access/photographer/dramatic)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np
import pytest
from astral import Observer

from app.analyze_location import (
    DemInfo,
    Diagnostics,
    LocationAnalysis,
    LocationInfo,
    SolarInfo,
    TerrainInfo,
    VisibilityInfo,
)
from app.horizon import HorizonProfile
from app.personalization import UserPreferences, compute_user_match, estimate_access_level
from app.photography_score import compute_photography_score
from app.sun_position_score import compute_sun_position_score
from app.sunset_score import classify_sunset_type, compute_full_sunset_score
from app.visibility_score import compute_visibility_score
from app.weather_provider import WeatherInfo
from app.weather_score import compute_atmosphere_score, compute_weather_score


def _analysis(horizon_at_sunset_deg, delay_minutes, sector_open_fraction=1.0, duration_minutes=90.0, elevation_m=1300.0):
    azimuths = list(np.arange(0.0, 360.0, 1.0))
    # sektor kolem 270° (sunset_azimuth) otevřený/zavřený podle sector_open_fraction
    altitudes = []
    for a in azimuths:
        diff = abs(((a - 270.0 + 180) % 360) - 180)
        if diff <= 20:
            altitudes.append(-2.0 if sector_open_fraction >= 0.5 else 10.0)
        else:
            altitudes.append(-1.0)
    profile = HorizonProfile(azimuths_deg=np.array(azimuths), horizon_altitude_deg=np.array(altitudes))
    terrain = TerrainInfo(
        azimuths_deg=azimuths, horizon_altitudes_deg=altitudes, minimum_horizon_deg=min(altitudes),
        maximum_horizon_deg=max(altitudes), slope_deg=3.0, aspect_deg=270.0, _profile=profile,
    )
    dem = DemInfo(provider="cuzk_dmr5g", dataset="ČÚZK DMR 5G (DTM)", resolution_m=10.0, analysis_range_m=50000.0,
                  source_crs="EPSG:4326", acquired_at=None, fallback_used=False, fallback_reason=None, quality_flag="ok")
    location = LocationInfo(latitude=49.5, longitude=18.4, elevation_m=elevation_m)
    sunset_time = datetime(2026, 9, 25, 18, 43, tzinfo=timezone.utc)
    solar = SolarInfo(
        query_time=sunset_time, current_azimuth_deg=200.0, current_altitude_deg=10.0,
        astronomical_sunset=sunset_time, sunset_azimuth_deg=270.0, sunset_terrain_horizon_deg=horizon_at_sunset_deg,
        astronomical_sunrise=None, sunrise_azimuth_deg=None, sunrise_terrain_horizon_deg=None,
    )
    visibility = VisibilityInfo(
        terrain_obstruction=delay_minutes > 0, astronomical_sunset=sunset_time,
        terrain_obscured_sunset=sunset_time + timedelta(minutes=delay_minutes), delay_minutes=delay_minutes,
        visibility_duration=timedelta(minutes=duration_minutes), visibility_duration_is_lower_bound=False,
        sunrise_terrain_obstruction=None, terrain_revealed_sunrise=None, sunrise_delay_minutes=None,
    )
    return LocationAnalysis(location=location, dem=dem, terrain=terrain, solar=solar, visibility=visibility, diagnostics=Diagnostics())


def _weather(cloud_total=20.0, cloud_low=5.0, cloud_mid=10.0, cloud_high=10.0, visibility_m=35000.0,
             precip_prob=0.0, precip_mm=0.0, humidity=50.0):
    return WeatherInfo(
        latitude=49.5, longitude=18.4, forecast_time=datetime(2026, 9, 25, 19, tzinfo=timezone.utc),
        cloud_cover_pct=cloud_total, cloud_cover_low_pct=cloud_low, cloud_cover_mid_pct=cloud_mid, cloud_cover_high_pct=cloud_high,
        visibility_m=visibility_m, precipitation_probability_pct=precip_prob, precipitation_mm=precip_mm,
        temperature_c=15.0, wind_speed_kmh=10.0, wind_direction_deg=270.0, humidity_pct=humidity, pressure_hpa=1013.0,
        weather_code=0, weather_condition="clear sky",
    )


OBSERVER = Observer(latitude=49.5, longitude=18.4, elevation=1300.0)
SUNSET_TIME = datetime(2026, 9, 25, 18, 43, tzinfo=timezone.utc)


# --- Scénář: Perfect terrain + good weather ---
def test_perfect_terrain_and_good_weather_gives_high_score():
    analysis = _analysis(horizon_at_sunset_deg=-3.0, delay_minutes=0.0, sector_open_fraction=1.0, duration_minutes=120.0)
    weather = _weather(cloud_total=25.0, cloud_low=5.0, cloud_mid=15.0, cloud_high=15.0)
    result = compute_full_sunset_score(analysis, weather, OBSERVER, SUNSET_TIME, elevation_m=1300.0)
    assert result.overall_0_100 >= 80


# --- Scénář: Blocked horizon + perfect weather ---
def test_blocked_horizon_with_perfect_weather_is_not_excellent():
    analysis = _analysis(horizon_at_sunset_deg=25.0, delay_minutes=55.0, sector_open_fraction=0.0, duration_minutes=10.0)
    weather = _weather(cloud_total=15.0, cloud_low=0.0, cloud_mid=5.0, cloud_high=10.0)
    result = compute_full_sunset_score(analysis, weather, OBSERVER, SUNSET_TIME, elevation_m=1300.0)
    # terrain je jen 25% váhy, ale blokovaný horizont musí výsledek reálně srazit dolů
    assert result.overall_0_100 < 75


# --- Scénář: Excellent terrain + heavy rain ---
def test_excellent_terrain_with_heavy_rain_is_penalized():
    analysis = _analysis(horizon_at_sunset_deg=-3.0, delay_minutes=0.0, sector_open_fraction=1.0, duration_minutes=120.0)
    weather = _weather(cloud_total=95.0, cloud_low=90.0, cloud_mid=80.0, cloud_high=60.0, precip_prob=90.0, precip_mm=5.0, visibility_m=4000.0)
    result_good = compute_full_sunset_score(_analysis(-3.0, 0.0, 1.0, 120.0), _weather(), OBSERVER, SUNSET_TIME, 1300.0)
    result_rain = compute_full_sunset_score(analysis, weather, OBSERVER, SUNSET_TIME, elevation_m=1300.0)
    assert result_rain.overall_0_100 < result_good.overall_0_100


# --- Scénář: Excellent visibility + low clouds ---
def test_low_clouds_hurt_weather_score_even_with_excellent_visibility():
    weather = _weather(cloud_total=70.0, cloud_low=70.0, cloud_mid=0.0, cloud_high=0.0, visibility_m=40000.0)
    weather_score = compute_weather_score(weather)
    visibility_score = compute_visibility_score(weather)
    assert visibility_score.score_0_100 >= 95  # dohlednost je skvělá
    assert weather_score.score_0_100 < 50  # ale nízká oblačnost přímo blokuje horizont


# --- Scénář: High clouds + clear horizon (mid/high oblačnost = "drama", ne penalizace) ---
def test_high_clouds_with_clear_low_layer_scores_well_not_poorly():
    weather = _weather(cloud_total=45.0, cloud_low=0.0, cloud_mid=25.0, cloud_high=20.0)
    result = compute_weather_score(weather)
    baseline_clear = compute_weather_score(_weather(cloud_total=0.0, cloud_low=0.0, cloud_mid=0.0, cloud_high=0.0))
    assert result.score_0_100 > baseline_clear.score_0_100  # střední oblačnost > čisté nebe (drama bonus)


# --- Zpětná vazba (živé použití, Render Free produkce) - "appka řekne
# cloudy, ale skóre ukazuje ~90 %". Barevnost (drama_score) dřív měla
# skoro stejnou váhu jako samotná viditelnost (nízká oblačnost) - appka
# tak uměla ukázat vysoké skóre i při zatažené obloze. Viditelnost teď
# musí být JASNĚ dominantní nad barevností. ---
def test_heavy_low_cloud_overcast_scores_poorly_even_with_favorable_mid_high_band():
    # Nízká oblačnost téměř úplně zatažená (přesně "appka řekne cloudy")
    # - i kdyby střední/vysoká oblačnost náhodou padla do "ideálního"
    # pásma pro drama_score, celková komponenta musí zůstat nízká,
    # protože o VIDITELNOSTI samotné rozhoduje hlavně low cloud.
    weather = _weather(cloud_total=90.0, cloud_low=90.0, cloud_mid=40.0, cloud_high=40.0)
    result = compute_weather_score(weather)
    assert result.score_0_100 <= 30


def test_visibility_dominates_over_color_drama_in_weighting():
    # Stejná "ideální" barevnost (mid/high ~35%) ve dvou scénářích -
    # jediný rozdíl je nízká oblačnost (otevřený vs. zatažený horizont).
    # Rozdíl ve výsledku musí být VELKÝ (viditelnost dominuje), ne jen
    # mírný posun (jako při staré 50/35/15 váze).
    open_horizon = compute_weather_score(_weather(cloud_total=35.0, cloud_low=5.0, cloud_mid=20.0, cloud_high=15.0))
    overcast = compute_weather_score(_weather(cloud_total=90.0, cloud_low=85.0, cloud_mid=20.0, cloud_high=15.0))
    assert open_horizon.score_0_100 - overcast.score_0_100 > 40


def test_moderate_clouds_with_open_horizon_still_scores_better_than_perfectly_clear():
    # Zpětná vazba - "když je hezky, ale na obloze jsou mraky, bude to
    # pěkný západ" - tenhle bonus NESMÍ zmizet, jen dostal menší váhu.
    # Nízká oblačnost (co by viditelnost) musí zůstat STEJNÁ (0) v obou
    # scénářích - jinak by test neporovnával barevnost samotnou (viz
    # test_visibility_dominates_over_color_drama_in_weighting výš, kde
    # se nízká oblačnost záměrně LIŠÍ).
    moderate = compute_weather_score(_weather(cloud_total=35.0, cloud_low=0.0, cloud_mid=20.0, cloud_high=15.0))
    perfectly_clear = compute_weather_score(_weather(cloud_total=0.0, cloud_low=0.0, cloud_mid=0.0, cloud_high=0.0))
    assert moderate.score_0_100 > perfectly_clear.score_0_100


def test_actively_raining_hard_caps_weather_score_regardless_of_other_factors():
    # Zpětná vazba - "když má pršet, je jasné že není [vidět]" - i při
    # jinak otevřeném horizontu a "ideální" barevnosti musí aktivní
    # srážka (ne jen pravděpodobnost) srazit CELOU komponentu na nízký
    # strop, ne jen o pár bodů.
    weather = _weather(cloud_total=35.0, cloud_low=5.0, cloud_mid=20.0, cloud_high=15.0, precip_prob=80.0, precip_mm=2.0)
    result = compute_weather_score(weather)
    assert result.score_0_100 <= 15
    assert "raining" in result.detail


def test_high_precipitation_probability_without_actual_rain_is_not_hard_capped():
    # Jen PRAVDĚPODOBNOST srážek (žádná aktuálně měřená srážka) je
    # pořád jen měkká penalizace (15% podsložka), ne tvrdý strop - ten
    # je vyhrazený pro SKUTEČNĚ probíhající srážku.
    weather = _weather(cloud_total=35.0, cloud_low=5.0, cloud_mid=20.0, cloud_high=15.0, precip_prob=80.0, precip_mm=0.0)
    result = compute_weather_score(weather)
    assert result.score_0_100 > 15


# --- Scénář: No weather data ---
def test_no_weather_data_reweights_instead_of_faking():
    analysis = _analysis(horizon_at_sunset_deg=-3.0, delay_minutes=0.0, sector_open_fraction=1.0, duration_minutes=90.0)
    result = compute_full_sunset_score(analysis, None, OBSERVER, SUNSET_TIME, elevation_m=1300.0)
    weather_component = next(c for c in result.components if c.key == "weather")
    assert weather_component.value_0_100 is None
    assert weather_component.availability == "unavailable"
    assert result.overall_0_100 > 0  # terrain+sun_position pořád dají smysluplné skóre
    assert result.sunset_type.key is None  # bez počasí žádný badge (ne "unknown" zobrazený jako typ)


def test_score_is_deterministic_and_reproducible():
    analysis = _analysis(horizon_at_sunset_deg=-1.0, delay_minutes=3.0, sector_open_fraction=0.8, duration_minutes=75.0)
    weather = _weather()
    r1 = compute_full_sunset_score(analysis, weather, OBSERVER, SUNSET_TIME, elevation_m=1300.0)
    r2 = compute_full_sunset_score(analysis, weather, OBSERVER, SUNSET_TIME, elevation_m=1300.0)
    assert r1.overall_0_100 == r2.overall_0_100
    assert [c.value_0_100 for c in r1.components] == [c.value_0_100 for c in r2.components]


def test_component_weights_sum_to_one():
    from app.sunset_score import WEIGHTS
    assert abs(sum(WEIGHTS.values()) - 1.0) < 1e-9


# --- Sun position score ---
def test_sun_position_score_slower_descent_scores_higher():
    from datetime import timedelta as td
    summer_obs = Observer(latitude=50.0, longitude=15.0, elevation=500.0)
    fast = compute_sun_position_score(summer_obs, datetime(2026, 3, 20, 17, 45, tzinfo=timezone.utc))  # blízko rovnodennosti
    slow = compute_sun_position_score(summer_obs, datetime(2026, 6, 21, 19, 0, tzinfo=timezone.utc))  # blízko slunovratu
    assert slow.descent_rate_deg_per_min < fast.descent_rate_deg_per_min
    assert slow.score_0_100 > fast.score_0_100


# --- Photography score ---
def test_photography_score_reweights_without_weather():
    with_weather = compute_photography_score(90.0, 80.0, 70.0)
    without_weather = compute_photography_score(90.0, None, 70.0)
    assert without_weather.score_0_100 is not None
    assert "no weather data" in without_weather.detail


# --- Sunset type classification ---
def test_classify_sunset_type_dramatic():
    weather = _weather(cloud_total=45.0, cloud_low=10.0, cloud_mid=25.0, cloud_high=20.0)
    result = classify_sunset_type(weather, elevation_m=400.0, terrain_score_0_100=60.0, photography_score_0_100=50.0)
    assert result.key == "dramatic"
    assert "favor" in result.confidence_text.lower()  # zadání bod 12 - "favor", ne "will be"


def test_classify_sunset_type_clear():
    weather = _weather(cloud_total=5.0, cloud_low=0.0, cloud_mid=3.0, cloud_high=2.0)
    result = classify_sunset_type(weather, elevation_m=400.0, terrain_score_0_100=60.0, photography_score_0_100=50.0)
    assert result.key == "clear"


def test_classify_sunset_type_cloudscape():
    weather = _weather(cloud_total=95.0, cloud_low=80.0, cloud_mid=60.0, cloud_high=40.0)
    result = classify_sunset_type(weather, elevation_m=400.0, terrain_score_0_100=60.0, photography_score_0_100=50.0)
    assert result.key == "cloudscape"


def test_classify_sunset_type_mountain_takes_priority_at_high_elevation():
    weather = _weather(cloud_total=20.0)
    result = classify_sunset_type(weather, elevation_m=1300.0, terrain_score_0_100=95.0, photography_score_0_100=50.0)
    assert result.key == "mountain"


def test_classify_sunset_type_none_without_weather():
    result = classify_sunset_type(None, elevation_m=1300.0, terrain_score_0_100=95.0, photography_score_0_100=50.0)
    assert result.key is None
    assert result.label is None


def test_sunset_type_never_claims_certainty():
    for weather_kwargs in [{"cloud_total": 45, "cloud_low": 10, "cloud_mid": 25, "cloud_high": 20}, {"cloud_total": 5}]:
        weather = _weather(**weather_kwargs)
        result = classify_sunset_type(weather, elevation_m=400.0, terrain_score_0_100=60.0, photography_score_0_100=50.0)
        assert "will be" not in result.confidence_text.lower()


# --- Personalization / user preferences ---
def test_no_preferences_gives_no_match_percentage():
    result = compute_user_match(UserPreferences(), "peak", 1300.0, 5.0, "clear", 80.0)
    assert result.match_pct is None  # None != 0% - zadání bod 15, "ukázat rozdíl"


def test_user_prefers_hiking_matches_far_candidate():
    # elevation_m=300.0 (pod ELEVATION_MODERATE_FLOOR_M) - test cíleně
    # izoluje distance-based logiku, ne elevation floor (ten má vlastní
    # testy, viz test_estimate_access_level_high_elevation_overrides_short_distance).
    prefs = UserPreferences(hiking_effort="hike")
    close = compute_user_match(prefs, "peak", 300.0, distance_km=1.0, sunset_type_key="clear", photography_score_0_100=None)
    far = compute_user_match(prefs, "peak", 300.0, distance_km=20.0, sunset_type_key="clear", photography_score_0_100=None)
    assert far.match_pct > close.match_pct


def test_user_wants_easy_access_matches_close_candidate():
    prefs = UserPreferences(hiking_effort="easy")
    close = compute_user_match(prefs, "peak", 300.0, distance_km=1.5, sunset_type_key="clear", photography_score_0_100=None)
    far = compute_user_match(prefs, "peak", 300.0, distance_km=30.0, sunset_type_key="clear", photography_score_0_100=None)
    assert close.match_pct > far.match_pct


def test_photographer_preference_uses_real_photography_score():
    prefs = UserPreferences(photography=True)
    high = compute_user_match(prefs, "peak", 1300.0, 5.0, "dramatic", photography_score_0_100=90.0)
    low = compute_user_match(prefs, "peak", 1300.0, 5.0, "dramatic", photography_score_0_100=20.0)
    assert high.match_pct > low.match_pct


def test_dramatic_preference_matches_dramatic_sunset_type():
    prefs = UserPreferences(dramatic=True)
    matching = compute_user_match(prefs, "peak", 1300.0, 5.0, sunset_type_key="dramatic", photography_score_0_100=None)
    not_matching = compute_user_match(prefs, "peak", 1300.0, 5.0, sunset_type_key="clear", photography_score_0_100=None)
    assert matching.match_pct > not_matching.match_pct


def test_scene_preference_without_environment_signal_is_marked_unavailable():
    # Milestone "Complete Product Build" (pokračování) - Water/Forest/City
    # MAJÍ dnes reálný signál (app/environment_context.py), ale jen když
    # ho volající předá (starší/nekompletní candidate data -> `None` ->
    # poctivě "nevíme", ne fake negativní match).
    prefs = UserPreferences(scene_types=["water"])
    result = compute_user_match(prefs, "peak", 1300.0, 5.0, "clear", None)
    assert "water" in result.unavailable_preferences
    assert result.match_pct is None  # jediná preference byla bez signálu -> žádný match


def test_water_preference_uses_real_environment_signal():
    prefs = UserPreferences(scene_types=["water"])
    near = compute_user_match(prefs, "peak", 1300.0, 5.0, "clear", None, near_water=True)
    far = compute_user_match(prefs, "peak", 1300.0, 5.0, "clear", None, near_water=False)
    assert near.match_pct > far.match_pct
    assert not near.unavailable_preferences


def test_forest_preference_uses_real_environment_signal():
    prefs = UserPreferences(scene_types=["forest"])
    near = compute_user_match(prefs, "peak", 1300.0, 5.0, "clear", None, near_forest=True)
    far = compute_user_match(prefs, "peak", 1300.0, 5.0, "clear", None, near_forest=False)
    assert near.match_pct > far.match_pct


def test_city_preference_uses_real_environment_signal():
    prefs = UserPreferences(scene_types=["city"])
    near = compute_user_match(prefs, "peak", 1300.0, 5.0, "clear", None, near_city=True)
    far = compute_user_match(prefs, "peak", 1300.0, 5.0, "clear", None, near_city=False)
    assert near.match_pct > far.match_pct


# --- Zpětná vazba (živé použití, Brno/Špilberk) - "to vůbec není
# přírodní, když je to uprostřed Brna" ---


def test_forest_near_city_is_not_counted_as_natural_match():
    # Špilberk-like kandidát: near_forest=True (malý park v okruhu),
    # ale ZÁROVEŇ near_city=True (0.8 km od centra) - "Natural"
    # preference (scene_types obsahuje "forest") tohle nesmí brát jako
    # shodu, protože dominantní charakter místa je městský.
    prefs = UserPreferences(scene_types=["forest"])
    urban_forest = compute_user_match(prefs, "viewpoint", 300.0, 1.0, "clear", None, near_forest=True, near_city=True)
    rural_forest = compute_user_match(prefs, "viewpoint", 300.0, 1.0, "clear", None, near_forest=True, near_city=False)
    no_forest = compute_user_match(prefs, "viewpoint", 300.0, 1.0, "clear", None, near_forest=False, near_city=False)
    assert urban_forest.match_pct == no_forest.match_pct  # městský "les" se počítá stejně jako žádný
    assert rural_forest.match_pct > urban_forest.match_pct
    assert "Matches your forest surroundings preference." not in urban_forest.reasons


def test_water_near_city_is_not_counted_as_natural_match():
    prefs = UserPreferences(scene_types=["water"])
    urban_water = compute_user_match(prefs, "viewpoint", 300.0, 1.0, "clear", None, near_water=True, near_city=True)
    rural_water = compute_user_match(prefs, "viewpoint", 300.0, 1.0, "clear", None, near_water=True, near_city=False)
    assert rural_water.match_pct > urban_water.match_pct


def test_city_preference_still_matches_when_also_near_forest_or_water():
    # Opravná podmínka se týká JEN "water"/"forest" - "city" preference
    # musí dál fungovat normálně, i když je zároveň near_forest/near_water.
    prefs = UserPreferences(scene_types=["city"])
    result = compute_user_match(prefs, "viewpoint", 300.0, 1.0, "clear", None, near_city=True, near_forest=True, near_water=True)
    assert result.match_pct == 100.0
    assert "Matches your cityscape preference." in result.reasons


def test_mountain_preference_matches_high_peak():
    prefs = UserPreferences(scene_types=["mountain"])
    mountain = compute_user_match(prefs, "peak", 1300.0, 5.0, "clear", None)
    lowland = compute_user_match(prefs, "viewpoint", 300.0, 5.0, "clear", None)
    assert mountain.match_pct > lowland.match_pct


# --- Milník 12 (zadání bod 4) - čtyři nové preference ---
def test_vivid_colors_preference_matches_vivid_color_potential():
    prefs = UserPreferences(vivid_colors=True)
    vivid = compute_user_match(prefs, "peak", 1300.0, 5.0, "clear", None, color_potential_key="vivid")
    muted = compute_user_match(prefs, "peak", 1300.0, 5.0, "clear", None, color_potential_key="muted")
    assert vivid.match_pct > muted.match_pct


def test_vivid_colors_preference_without_weather_is_marked_unavailable():
    prefs = UserPreferences(vivid_colors=True)
    result = compute_user_match(prefs, "peak", 1300.0, 5.0, "clear", None, color_potential_key=None)
    assert "vivid_colors" in result.unavailable_preferences
    assert result.match_pct is None


def test_inversion_preference_matches_favorable_inversion_potential():
    prefs = UserPreferences(inversion=True)
    favorable = compute_user_match(prefs, "peak", 1300.0, 5.0, "clear", None, inversion_key="favorable")
    unlikely = compute_user_match(prefs, "peak", 1300.0, 5.0, "clear", None, inversion_key="unlikely")
    assert favorable.match_pct > unlikely.match_pct


def test_inversion_preference_without_weather_is_marked_unavailable():
    prefs = UserPreferences(inversion=True)
    result = compute_user_match(prefs, "peak", 1300.0, 5.0, "clear", None, inversion_key=None)
    assert "inversion" in result.unavailable_preferences
    assert result.match_pct is None


def test_sun_on_horizon_preference_uses_real_terrain_component_directly():
    # Zadání "znovupoužij existující terénní data" - žádné binární
    # prahování navíc, přímo použité existující 0-100 terénní skóre.
    prefs = UserPreferences(sun_on_horizon=True)
    open_horizon = compute_user_match(prefs, "peak", 1300.0, 5.0, "clear", None, horizon_at_azimuth_0_100=95.0)
    blocked = compute_user_match(prefs, "peak", 1300.0, 5.0, "clear", None, horizon_at_azimuth_0_100=10.0)
    assert open_horizon.match_pct > blocked.match_pct
    assert open_horizon.match_pct == 95  # přímé znovupoužití hodnoty, žádné přepočítávání


def test_panorama_preference_uses_real_sector_openness_component_directly():
    prefs = UserPreferences(panorama=True)
    wide = compute_user_match(prefs, "peak", 1300.0, 5.0, "clear", None, sector_openness_0_100=90.0)
    narrow = compute_user_match(prefs, "peak", 1300.0, 5.0, "clear", None, sector_openness_0_100=15.0)
    assert wide.match_pct > narrow.match_pct


def test_new_preferences_never_silently_change_base_sunset_score():
    # Zadání "preference nesmí tajně přepisovat základní skóre" - tyhle
    # testy ověřují jen `compute_user_match()`, což je ZÁMĚRNĚ samostatná
    # funkce, co nikdy nebere ani nevrací `sunset_score`/`overall_0_100`.
    import inspect
    sig = inspect.signature(compute_user_match)
    assert "sunset_score" not in sig.parameters
    assert "overall_0_100" not in sig.parameters


def test_estimate_access_level_boundaries():
    assert estimate_access_level(1.0) == "easy"
    assert estimate_access_level(5.0) == "moderate"
    assert estimate_access_level(15.0) == "hike"
    assert estimate_access_level(40.0) == "adventure"


def test_estimate_access_level_high_elevation_overrides_short_distance():
    # BUG NALEZENÝ živě (Lysá hora, 1324 m, nejvyšší vrchol Beskyd,
    # vyšla jako "easy access" jen kvůli krátké vzdušné vzdálenosti od
    # hledaného středu) - elevace musí fungovat jako floor.
    assert estimate_access_level(2.0, elevation_m=1324.0) == "hike"
    assert estimate_access_level(2.0, elevation_m=900.0) == "moderate"
    assert estimate_access_level(2.0, elevation_m=400.0) == "easy"  # nízká elevace beze změny


def test_estimate_access_level_elevation_never_downgrades_a_harder_distance_estimate():
    # elevace je jen FLOOR (nikdy nesnižuje už tak vysoký odhad z distance)
    assert estimate_access_level(40.0, elevation_m=200.0) == "adventure"


def test_estimate_access_level_without_elevation_is_unaffected():
    assert estimate_access_level(2.0) == "easy"  # zpětně kompatibilní výchozí chování (elevation_m=None)


# --- Atmosphere score ---
def test_atmosphere_score_unavailable_without_weather():
    result = compute_atmosphere_score(None)
    assert result.score_0_100 is None
