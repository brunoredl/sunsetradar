"""Testy pro app/sunset_score.py - Milník "Postupné vylepšení" (Fáze 2):
`classify_general_sky()`/`classify_general_horizon_view()`. Čisté funkce,
žádná síť/DB - ověřují, že "obecná" klasifikace je skutečně NEZÁVISLÁ na
terénu (nikdy nevybere "mountain"/"photography", i když by to terén-
specifická `classify_sunset_type()` se stejným počasím udělala) a že
"přímý výhled na slunce" je čistě z nízké oblačnosti, bez vymyšlených
procent."""

from __future__ import annotations

from datetime import datetime, timezone

from app.sunset_score import (
    HorizonViewResult,
    classify_color_potential,
    classify_general_horizon_view,
    classify_general_inversion_potential,
    classify_general_sky,
    classify_inversion_potential,
    classify_sunset_type,
    estimate_dew_point_c,
)
from app.weather_provider import WeatherInfo


def _weather(**overrides) -> WeatherInfo:
    base = dict(
        latitude=50.0, longitude=14.0, forecast_time=datetime(2026, 10, 18, 17, 0, tzinfo=timezone.utc),
        cloud_cover_pct=20.0, cloud_cover_low_pct=10.0, cloud_cover_mid_pct=15.0, cloud_cover_high_pct=10.0,
        visibility_m=35000.0, precipitation_probability_pct=0.0, precipitation_mm=0.0, temperature_c=15.0,
        wind_speed_kmh=8.0, wind_direction_deg=270.0, humidity_pct=55.0, pressure_hpa=1013.0,
        weather_code=1, weather_condition="mainly clear",
    )
    base.update(overrides)
    return WeatherInfo(**base)


def test_classify_general_sky_returns_none_without_weather():
    result = classify_general_sky(None)
    assert result.key is None
    assert result.label is None


def test_classify_general_sky_never_picks_mountain_even_at_mountain_like_weather():
    # Zadání "obecná předpověď... nezávislá na konkrétním vyhlídkovém
    # místě" - terén-specifická `classify_sunset_type()` by se stejným
    # počasím A vysokou elevací/terén skóre vybrala "mountain", ale
    # `classify_general_sky()` nemá žádnou elevaci/terén k dispozici
    # (volá se s elevation_m=0, terrain_score=0), takže "mountain" nikdy
    # nevybere - ověřeno PŘÍMÝM porovnáním obou funkcí nad stejným počasím.
    weather = _weather(cloud_cover_low_pct=10.0, cloud_cover_mid_pct=30.0, cloud_cover_high_pct=30.0)
    terrain_aware = classify_sunset_type(weather, elevation_m=1500.0, terrain_score_0_100=95.0, photography_score_0_100=None)
    general = classify_general_sky(weather)
    assert terrain_aware.key == "mountain"
    assert general.key != "mountain"


def test_classify_general_sky_never_picks_photography_without_photo_score():
    weather = _weather(humidity_pct=40.0)
    general = classify_general_sky(weather)
    assert general.key != "photography"


def test_classify_general_sky_matches_terrain_aware_for_low_elevation_point():
    # Pro bod BEZ terénní "mountain" kvalifikace musí obecná a
    # terén-specifická klasifikace dát STEJNÝ výsledek (obě volají
    # stejnou funkci, jen s jinými terén-vstupy, které se v tomhle
    # případě na výsledek neprojeví).
    weather = _weather(cloud_cover_pct=10.0, cloud_cover_low_pct=5.0, cloud_cover_mid_pct=5.0, cloud_cover_high_pct=5.0)
    terrain_aware = classify_sunset_type(weather, elevation_m=300.0, terrain_score_0_100=40.0, photography_score_0_100=None)
    general = classify_general_sky(weather)
    assert general.key == terrain_aware.key == "clear"


def test_classify_general_horizon_view_returns_none_without_weather():
    result = classify_general_horizon_view(None)
    assert result == HorizonViewResult(None, None, None)


def test_classify_general_horizon_view_likely_for_low_clouds():
    result = classify_general_horizon_view(_weather(cloud_cover_low_pct=5.0))
    assert result.key == "likely"
    assert result.low_cloud_pct == 5.0


def test_classify_general_horizon_view_partial_for_mid_clouds():
    result = classify_general_horizon_view(_weather(cloud_cover_low_pct=45.0))
    assert result.key == "partial"


def test_classify_general_horizon_view_unlikely_for_heavy_low_clouds():
    result = classify_general_horizon_view(_weather(cloud_cover_low_pct=90.0))
    assert result.key == "unlikely"


def test_classify_general_horizon_view_never_fabricates_a_percentage_label():
    # Zadání "nevymýšlej procenta" - label je vždy kvalitativní text,
    # nikdy obsahuje číslo/%.
    for low in (5.0, 45.0, 90.0):
        result = classify_general_horizon_view(_weather(cloud_cover_low_pct=low))
        assert "%" not in result.label


# ============================= Milník 12 (zadání bod 2) -
# classify_color_potential() =============================


def test_classify_color_potential_returns_none_without_weather():
    result = classify_color_potential(None)
    assert result.key is None
    assert result.label is None
    assert result.detail is None


def test_classify_color_potential_vivid_for_scattered_mid_high_cloud_and_open_horizon():
    weather = _weather(cloud_cover_low_pct=10.0, cloud_cover_mid_pct=40.0, cloud_cover_high_pct=40.0)
    result = classify_color_potential(weather)
    assert result.key == "vivid"


def test_classify_color_potential_muted_for_heavy_overcast():
    weather = _weather(cloud_cover_low_pct=80.0, cloud_cover_pct=95.0)
    result = classify_color_potential(weather)
    assert result.key == "muted"


def test_classify_color_potential_clear_sky_is_warm_not_vivid():
    # Zadání "úplně jasná obloha nemusí znamenat nejvýraznější barvy" -
    # čistě jasná obloha (žádné mraky vůbec) musí dát "warm", NE "vivid"
    # (ten vyžaduje rozptýlené střední/vysoké mraky jako plátno pro barvu).
    weather = _weather(cloud_cover_pct=5.0, cloud_cover_low_pct=2.0, cloud_cover_mid_pct=2.0, cloud_cover_high_pct=1.0)
    result = classify_color_potential(weather)
    assert result.key == "warm"
    assert result.key != "vivid"


def test_classify_color_potential_never_fabricates_a_percentage_label():
    for low, mid, high, total in [(10, 40, 40, 90), (80, 10, 10, 95), (2, 2, 1, 5)]:
        weather = _weather(cloud_cover_low_pct=low, cloud_cover_mid_pct=mid, cloud_cover_high_pct=high, cloud_cover_pct=total)
        result = classify_color_potential(weather)
        assert "%" not in result.label


# ============================= Milník 12 (zadání bod 3) -
# estimate_dew_point_c() / classify_inversion_potential() =============================


def test_estimate_dew_point_c_equals_temperature_at_full_saturation():
    # Při 100% relativní vlhkosti je rosný bod == teplota (standardní
    # meteorologická vlastnost, dobrá kontrola správnosti aproximace).
    assert estimate_dew_point_c(12.0, 100.0) == 12.0


def test_estimate_dew_point_c_drops_below_temperature_as_humidity_falls():
    dp_humid = estimate_dew_point_c(15.0, 90.0)
    dp_dry = estimate_dew_point_c(15.0, 40.0)
    assert dp_dry < dp_humid < 15.0


def test_classify_inversion_potential_returns_none_without_weather():
    result = classify_inversion_potential(None, elevation_m=1000.0)
    assert result.key is None
    assert result.label is None


def test_classify_inversion_potential_favorable_for_elevated_point_with_low_fog_and_clear_above():
    weather = _weather(cloud_cover_low_pct=70.0, cloud_cover_mid_pct=10.0, cloud_cover_high_pct=10.0,
                        temperature_c=5.0, humidity_pct=95.0)
    result = classify_inversion_potential(weather, elevation_m=1200.0)
    assert result.key == "favorable"


def test_classify_inversion_potential_possible_not_favorable_for_low_elevation_point():
    # Zadání "netvrď, že konkrétní vrchol bude určitě nad mlhou" - STEJNÉ
    # počasí jako u "favorable" testu výše, ale nízká elevace bodu ->
    # nesmí dostat "favorable" (ten vyžaduje i elevaci), jen "possible".
    weather = _weather(cloud_cover_low_pct=70.0, cloud_cover_mid_pct=10.0, cloud_cover_high_pct=10.0,
                        temperature_c=5.0, humidity_pct=95.0)
    result = classify_inversion_potential(weather, elevation_m=250.0)
    assert result.key == "possible"
    assert result.key != "favorable"


def test_classify_inversion_potential_unlikely_without_low_cloud_signal():
    weather = _weather(cloud_cover_low_pct=5.0, cloud_cover_mid_pct=5.0, cloud_cover_high_pct=5.0)
    result = classify_inversion_potential(weather, elevation_m=1200.0)
    assert result.key == "unlikely"


def test_classify_inversion_potential_uncertain_when_cloud_layer_is_thick_throughout():
    weather = _weather(cloud_cover_low_pct=70.0, cloud_cover_mid_pct=70.0, cloud_cover_high_pct=70.0,
                        temperature_c=5.0, humidity_pct=95.0)
    result = classify_inversion_potential(weather, elevation_m=1200.0)
    assert result.key == "uncertain"


def test_classify_inversion_potential_never_claims_certainty_in_label():
    # Zadání "nikdy jako potvrzená inverze" - label nikdy neobsahuje
    # jistotu naznačující slova.
    weather = _weather(cloud_cover_low_pct=70.0, cloud_cover_mid_pct=10.0, cloud_cover_high_pct=10.0,
                        temperature_c=5.0, humidity_pct=95.0)
    for elevation in (200.0, 700.0, 1500.0):
        result = classify_inversion_potential(weather, elevation_m=elevation)
        # "not guaranteed"/"not confirm..." negated phrasing is fine (and
        # expected - that's the honest disclaimer) - only bare/affirmative
        # certainty claims are forbidden.
        for forbidden in ("will be confirmed", "definitely", "is certain", "100%"):
            assert forbidden not in result.label.lower()
            assert forbidden not in (result.detail or "").lower()
        assert "not guaranteed" in result.detail.lower() or result.key in ("uncertain", "unlikely", "possible")


def test_classify_general_inversion_potential_independent_of_clicked_point_elevation():
    # Zadání "obecná předpověď... nezávislá na konkrétním vyhlídkovém
    # místě" - obecná verze musí dát STEJNÝ výsledek bez ohledu na to,
    # jestli byl právě vybraný bod nížinný, nebo horský (neutralizuje
    # elevaci, stejně jako classify_general_sky neutralizuje terén).
    weather = _weather(cloud_cover_low_pct=70.0, cloud_cover_mid_pct=10.0, cloud_cover_high_pct=10.0,
                        temperature_c=5.0, humidity_pct=95.0)
    general = classify_general_inversion_potential(weather)
    assert general.key == "favorable"
    low_point_specific = classify_inversion_potential(weather, elevation_m=200.0)
    assert general.key != low_point_specific.key
