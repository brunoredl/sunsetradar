"""Testy pro app/main.py (Fáze 5 - webová vrstva) - FastAPI TestClient.

Většina testů mockuje `analyze_location()` přímo (rychlé, deterministické -
ověřují jen ROUTING/serializaci/validaci/chybové stavy webové vrstvy, ne
znovu geometrii, která je otestovaná jinde). Pár `@pytest.mark.integration`
testů běží proti reálnému enginu."""

from __future__ import annotations

import json
import math
import pathlib
import re
from datetime import datetime, timedelta, timezone

import pytest
import requests
from fastapi.testclient import TestClient

import app.main as main_module
from app.analyze_location import (
    DemInfo,
    Diagnostics,
    LocationAnalysis,
    LocationInfo,
    SolarInfo,
    TerrainInfo,
    VisibilityInfo,
)
from app.candidate_discovery import haversine_m
from app.cuzk_dem import OutsideCoverageError
from app.dem import OutOfBoundsError
from app.horizon import HorizonProfile

client = TestClient(main_module.app)


def _fake_analysis() -> LocationAnalysis:
    azimuths = [float(i) for i in range(360)]
    altitudes = [-1.0 for _ in range(360)]
    profile = HorizonProfile(azimuths_deg=__import__("numpy").array(azimuths), horizon_altitude_deg=__import__("numpy").array(altitudes))

    sunset = datetime(2026, 6, 21, 19, 20, 0, tzinfo=timezone.utc)
    return LocationAnalysis(
        location=LocationInfo(latitude=50.5552, longitude=13.9312, elevation_m=835.3),
        dem=DemInfo(
            provider="cuzk_dmr5g", dataset="ČÚZK DMR 5G (DTM)", resolution_m=10.0, analysis_range_m=50000.0,
            source_crs="EPSG:4326", acquired_at=sunset, fallback_used=False, fallback_reason=None, quality_flag="ok",
        ),
        terrain=TerrainInfo(
            azimuths_deg=azimuths, horizon_altitudes_deg=altitudes, minimum_horizon_deg=-1.0, maximum_horizon_deg=-1.0,
            slope_deg=5.0, aspect_deg=180.0, _profile=profile,
        ),
        solar=SolarInfo(
            query_time=datetime(2026, 6, 21, 12, 0, 0, tzinfo=timezone.utc), current_azimuth_deg=150.0, current_altitude_deg=55.0,
            astronomical_sunset=sunset, sunset_azimuth_deg=272.0, sunset_terrain_horizon_deg=-1.0,
            astronomical_sunrise=None, sunrise_azimuth_deg=None, sunrise_terrain_horizon_deg=None,
        ),
        visibility=VisibilityInfo(
            terrain_obstruction=False, astronomical_sunset=sunset, terrain_obscured_sunset=sunset, delay_minutes=0.0,
            visibility_duration=timedelta(hours=3), visibility_duration_is_lower_bound=True,
            sunrise_terrain_obstruction=None, terrain_revealed_sunrise=None, sunrise_delay_minutes=None,
        ),
        diagnostics=Diagnostics(warnings=[], calculation_parameters={"analysis_range_m": 50000.0}),
    )


def _patch_analyze(monkeypatch, fn):
    monkeypatch.setattr(main_module, "analyze_location", fn)


@pytest.fixture(autouse=True)
def _no_real_weather_network_calls(monkeypatch):
    """Milestone "Complete Product Build" - `_serialize_analysis()`
    (volaná z `/api/analyze`) teď VŽDY volá `weather_cache.fetch_weather_cached()`
    (reálná síť, Open-Meteo). Autouse napříč celým souborem - testy
    webové vrstvy nezávisí na síti (stejný princip jako u DEM/Overpass/
    Nominatim mockování jinde v týhle testovací sadě). Weather-specifické
    chování má vlastní izolované testy v tests/test_weather.py."""
    from app.weather_provider import WeatherInfo

    def fake_fetch(lat, lon, target_time, timezone_name="UTC"):
        return WeatherInfo(
            latitude=lat, longitude=lon, forecast_time=target_time,
            cloud_cover_pct=20.0, cloud_cover_low_pct=5.0, cloud_cover_mid_pct=10.0, cloud_cover_high_pct=5.0,
            visibility_m=35000.0, precipitation_probability_pct=0.0, precipitation_mm=0.0, temperature_c=15.0,
            wind_speed_kmh=8.0, wind_direction_deg=270.0, humidity_pct=55.0, pressure_hpa=1013.0,
            weather_code=1, weather_condition="mainly clear",
        )

    monkeypatch.setattr(main_module.weather_cache, "fetch_weather_cached", fake_fetch)


@pytest.fixture(autouse=True)
def _no_real_environment_network_calls(monkeypatch):
    """Milestone "Data Foundation" - `_environment_signal_for_point()`
    (volaná z `/api/analyze` s preferencemi) teď hledá dlaždici CELÉ
    národní mřížky (`app.national_grid.generate_grid_cells()`), ne jen
    3 staré regiony - libovolný bod v ČR (třeba testovací Milešovka)
    proto padne do NĚJAKÉ dlaždice a bez mockování by zkoušel reálný
    Overpass fetch, pokud zrovna není nakešovaný na disku (stejný
    princip jako `_no_real_weather_network_calls` výše)."""
    from app.environment_context import EnvironmentFeatures

    monkeypatch.setattr(
        main_module,
        "discover_environment_features_cached",
        lambda region, bbox: EnvironmentFeatures(water_points=[], forest_points=[], city_points=[]),
    )


# --- Základní stránky ---


def test_index_page_loads():
    r = client.get("/")
    assert r.status_code == 200
    assert "Sunset Radar" in r.text
    assert "Milešovka" in r.text  # testovací lokality vyrenderované


def test_static_files_served():
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/static/style.css").status_code == 200


# --- Milestone "Sunset-first UI and Minimalist Map Redesign" ---


def test_index_page_has_compact_map_with_expand_control():
    # Zadání bod 1C - mapa musí být kompaktní a rozšiřitelná, ne hlavní
    # plocha stránky. Jen kontrola, že řídicí prvek skutečně existuje
    # v HTML (vizuální velikost samu o sobě ověřuje jen živý prohlížeč,
    # viz report - tohle je regresní pojistka na existenci ovládání).
    r = client.get("/")
    assert "map-expand-btn" in r.text


def test_index_page_has_dem_quality_note_placeholder():
    r = client.get("/")
    assert 'id="dem-quality-note"' in r.text


def test_explore_page_has_persistent_coverage_note_placeholder():
    r = client.get("/explore")
    assert r.status_code == 200
    assert 'id="explore-coverage-note"' in r.text


def test_place_page_has_dem_quality_note_placeholder(fake_catalog, fake_cache, fake_worker):
    r = client.get("/sunset/near/")
    assert r.status_code == 200
    assert 'id="place-dem-quality-note"' in r.text


@pytest.mark.parametrize("filename", ["app.js", "explore.js", "place.js"])
def test_map_style_uses_keyless_vector_source_not_raster_opentopomap(filename):
    # Regresní pojistka na skutečnou změnu zdroje mapových dlaždic
    # (zadání bod 2 - "monochromatický... bez placené služby/API klíče").
    # Ověřeno i živě přes `curl` (viz report) - tady jen hlídáme, že se
    # starý rastrový zdroj někdo omylem nevrátil zpátky.
    path = pathlib.Path(__file__).resolve().parent.parent / "static" / filename
    text = path.read_text()
    assert "tile.opentopomap.org" not in text  # starý rastrový tile zdroj (ne jen zmínka v komentáři historie)
    assert "tiles.openfreemap.org/styles/dark" in text


# --- Milestone "Visible Waiting Candidates" ---


def test_index_page_has_refresh_results_control():
    r = client.get("/")
    assert 'id="refresh-results-btn"' in r.text


def test_explore_page_has_refresh_results_control():
    r = client.get("/explore")
    assert 'id="explore-refresh-results-btn"' in r.text


@pytest.mark.parametrize("filename", ["app.js", "explore.js"])
def test_frontend_renders_other_candidates_not_just_results(filename):
    # Regresní pojistka na přesnou příčinu nahlášeného chování ("Brno,
    # 60 nalezeno, jen 1 výsledek"): frontend MUSÍ číst
    # `data.other_candidates`, ne jen `data.results` - jinak zbylí
    # nalezení kandidáti opět tiše zmizí z UI, i když je server teď
    # správně vrací.
    path = pathlib.Path(__file__).resolve().parent.parent / "static" / filename
    text = path.read_text()
    assert "other_candidates" in text


@pytest.mark.parametrize("filename", ["app.js", "explore.js"])
def test_frontend_retry_loop_does_not_stop_rendering_other_candidates(filename):
    # Přesná regrese na nalezenou chybu: starý retry loop přestal čekat
    # (správně) hned jak `ready_count >= 1`, ale vykresloval JEN
    # `data.results` - funkce, která výsledek vykresluje, teď musí
    # existovat a volat se i pro `ready_count === 0` větev (viz
    # `renderFindSunsetOutcome`), ne jen pro úspěšný případ.
    path = pathlib.Path(__file__).resolve().parent.parent / "static" / filename
    text = path.read_text()
    assert "renderFindSunsetOutcome" in text


# --- Milestone "Immediate Full Results" ---


@pytest.mark.parametrize("filename", ["app.js", "explore.js"])
def test_frontend_renders_immediately_without_blocking_retry_loop(filename):
    # Regresní pojistka na přesnou příčinu: PŘED touhle milestone frontend
    # čekal (blokující smyčka, až 40s) PŘED prvním vykreslením seznamu -
    # uživatel neviděl VŮBEC ŽÁDNÝ seznam, dokud smyčka neskončila.
    # `scheduleFindSunsetPoll` dokazuje, že doplňování teď běží AŽ PO
    # prvním (okamžitém) vykreslení, na pozadí, ne před ním.
    path = pathlib.Path(__file__).resolve().parent.parent / "static" / filename
    text = path.read_text()
    assert "scheduleFindSunsetPoll" in text
    assert "MAX_RETRIES" not in text  # starý blokující retry vzor se do kódu nevrátil


@pytest.mark.parametrize("filename", ["app.js", "explore.js"])
def test_frontend_search_uses_cancellation_token_against_stale_background_updates(filename):
    # Zadání bod "opakované hledání... bez duplicit": pozadí-dotaz z
    # PŘEDCHOZÍHO hledání nesmí přepsat aktuálně zobrazené výsledky
    # NOVĖJŠÍHO hledání - token zajišťuje, že se zahodí.
    path = pathlib.Path(__file__).resolve().parent.parent / "static" / filename
    text = path.read_text()
    assert "findSunsetSearchToken" in text


def test_find_sunset_request_accepts_other_candidates_offset_field(fake_catalog, fake_cache, fake_worker):
    # Zadání bod "musí existovat možnost zobrazit další" - request model
    # musí nové pole přijmout, ne ho tiše zahodit/vyhodit 422.
    r = client.post(
        "/api/find_sunset",
        json={"lat": 50.0, "lon": 14.0, "radius_km": 10, "other_candidates_offset": 5},
    )
    assert r.status_code == 200
    assert "other_candidates_next_offset" in r.json()


def test_find_sunset_other_candidates_status_vocabulary_distinguishes_evaluating_and_waiting(
    many_candidates, fake_cache, fake_worker,
):
    # Zadání bod "Vyhodnocuje se" (STATUS_RUNNING) vs. "Čeká na
    # vyhodnocení" (zařazeno, worker se ještě nedostal) - musí být
    # rozlišitelné stavy, ne jedno obecné "pending".
    cache_state, _ = fake_cache
    cache_state["node/0"] = "running"  # worker na tom PRÁVĚ TEĎ pracuje
    # "node/1" zůstává "missing" -> nově zařazeno, ještě nezačalo
    r = client.post("/api/find_sunset", json={"lat": 50.0, "lon": 14.0, "radius_km": 10})
    data = r.json()
    other = {c["id"]: c["status"] for c in data["other_candidates"]}
    assert other["node/0"] == "evaluating"
    assert other["node/1"] == "waiting"


def test_index_page_has_show_more_candidates_control():
    r = client.get("/")
    assert 'id="show-more-candidates-btn"' in r.text


def test_explore_page_has_show_more_candidates_control():
    r = client.get("/explore")
    assert 'id="explore-show-more-candidates-btn"' in r.text


@pytest.mark.parametrize("filename", ["app.js", "explore.js"])
def test_frontend_wires_up_show_more_pagination(filename):
    # Zadání bod "musí existovat možnost zobrazit další" - nejen
    # backendová podpora (`other_candidates_offset`), ale skutečná
    # klikatelná akce ve frontendu, ne jen tichý limit.
    path = pathlib.Path(__file__).resolve().parent.parent / "static" / filename
    text = path.read_text()
    assert "updateShowMoreButton" in text
    assert "other_candidates_offset" in text


# --- Milník "Ověření skutečného UI" - nálezy ze skutečného browser
# testu (Playwright, headless Chromium, viz report): staré výsledky
# nemizely po hledání, které selhalo DŘÍV, než se vůbec zavolalo
# /api/find_sunset (geocoding nenašel místo / spojení selhalo), a
# "Other sunsets" sekce natahovala stránku na tisíce pixelů u hustě
# pokrytých oblastí. Tady jen regresní pojistky na přítomnost opravy v
# kódu - skutečné chování bylo ověřeno živě v prohlížeči (viz report),
# ne jen tady (žádný JS test runner v projektu není). ---


@pytest.mark.parametrize("filename", ["app.js", "explore.js"])
def test_frontend_clears_stale_results_on_failed_search(filename):
    # AUDIT NÁLEZ (reálný browser test): hledání, které selže DŘÍV, než
    # se zavolá /api/find_sunset (geocoding 404, nebo síťová chyba),
    # dřív nechalo staré karty/hero z PŘEDCHOZÍHO úspěšného hledání
    # plně viditelné pod novou chybovou hláškou.
    path = pathlib.Path(__file__).resolve().parent.parent / "static" / filename
    text = path.read_text()
    assert "clearAllFindSunsetResults" in text or "clearStaleResults" in text
    # funkce musí být volaná v obou selhávajících větvích geocodingu
    # (nejen definovaná) - alespoň 2x mimo samotnou definici.
    marker = "clearAllFindSunsetResults" if "clearAllFindSunsetResults" in text else "clearStaleResults"
    assert text.count(marker) >= 3  # 1x definice + aspoň 2x volání (geocode 404, síťová chyba)


@pytest.mark.parametrize("filename", ["app.js"])
def test_frontend_collapses_long_other_sunsets_list(filename):
    # AUDIT NÁLEZ (reálný browser test, Brno @ 5 km): 59 karet natáhlo
    # stránku na ~7200 px - "zahlcení hlavní části stránky". Karty se
    # teď zobrazí jen do rozumného počtu, zbytek za klikem (čistě
    # klientské, žádný nový request).
    path = pathlib.Path(__file__).resolve().parent.parent / "static" / filename
    text = path.read_text()
    assert "COLLAPSED_OTHER_SUNSETS_LIMIT" in text
    assert "expand-other-sunsets-btn" in text


def test_index_page_has_expand_other_sunsets_control():
    r = client.get("/")
    assert 'id="expand-other-sunsets-btn"' in r.text


# --- Milník "Vizuální sjednocení" - nálezy ze skutečného browser testu
# (Playwright, headless Chromium): tři CSS třídy (`.place-status`,
# `.best-grid`, `.filter-chips`) nastavovaly `display` BEZ `:not([hidden])`
# - stejná specificita jako `[hidden]` z UA stylesheetu, autorské pravidlo
# (pozdější v cascade) vyhrálo, takže `hidden = true` z JS nemělo žádný
# vizuální efekt (ověřeno computed `display`/`getBoundingClientRect`
# v reálném prohlížeči, ne jen čtením zdrojového kódu). Tyhle testy jsou
# jen regresní pojistka na PŘÍTOMNOST opravy v CSS - skutečný vizuální
# efekt byl ověřen živě (viz report). ---


@pytest.mark.parametrize("selector", [".place-status", ".best-grid", ".filter-chips"])
def test_css_classes_toggled_via_hidden_respect_not_hidden_specificity(selector):
    css = pathlib.Path(__file__).resolve().parent.parent.joinpath("static", "style.css").read_text()
    assert f"{selector}:not([hidden])" in css, (
        f"{selector} nastavuje display mimo :not([hidden]) - hidden atribut z JS by byl tiše ignorován "
        "(stejná třída chyby jako historicky #loading/.hero-stat, viz komentáře v CSS)"
    )


def test_index_page_has_today_conditions_summary():
    # Milník 12 (zadání bod 1A) - dominantní headline/vysvětlení (barevný
    # potenciál) + doplňující badge řádek (horizont/inverze/jistota) -
    # jen přítomnost prvků v HTML, skutečné vyplnění reálnými daty bylo
    # ověřeno živě v prohlížeči (viz report).
    r = client.get("/")
    assert 'id="tonight-card"' in r.text
    assert 'id="tonight-headline"' in r.text
    assert 'id="tonight-explain"' in r.text
    assert 'id="today-conditions"' in r.text
    assert 'id="today-horizon-badge"' in r.text
    assert 'id="today-inversion-badge"' in r.text
    assert 'id="today-confidence-badge"' in r.text


def test_app_js_never_fabricates_confidence_percentage():
    # Zadání bod 3 - "nevymýšlej procenta... jistotu" - regresní pojistka,
    # že se do kódu nedostane vymyšlené číslo jistoty/pravděpodobnosti
    # vedle today-confidence-badge (text je vždy jen kvalitativní výhrada,
    # nikdy %).
    text = pathlib.Path(__file__).resolve().parent.parent.joinpath("static", "app.js").read_text()
    idx = text.index("today-confidence-badge")
    snippet = text[idx:idx + 400]
    assert "%" not in snippet


def test_details_grid_collapses_to_single_column_on_mobile():
    # AUDIT NÁLEZ (skutečný browser test, 390px viewport): `.details-grid
    # dt { grid-row: 1 }` nutilo grid vytvořit tolik sloupců, kolik je
    # položek v "Terrain" tabulce (7+), bez ohledu na dostupnou šířku -
    # na mobilu přetékalo přes okraj obrazovky (ověřeno živě přes
    # `getBoundingClientRect()`, ne jen čtením zdrojového kódu).
    css = pathlib.Path(__file__).resolve().parent.parent.joinpath("static", "style.css").read_text()
    media_start = css.index("@media (max-width: 640px)", css.index("RESPONSIVE"))
    media_block = css[media_start:media_start + 2000]
    assert ".details-grid" in media_block
    assert "grid-row: auto" in media_block


# --- Validace vstupu (zadání bod 19 - bezpečnost/robustnost) ---


@pytest.mark.parametrize("lat", [-91, 91, 1000, -1000])
def test_invalid_latitude_rejected(lat):
    r = client.post("/api/analyze", json={"lat": lat, "lon": 14.0, "date": "2026-06-21", "timezone": "UTC"})
    assert r.status_code == 422


@pytest.mark.parametrize("lon", [-181, 181, 999])
def test_invalid_longitude_rejected(lon):
    r = client.post("/api/analyze", json={"lat": 50.0, "lon": lon, "date": "2026-06-21", "timezone": "UTC"})
    assert r.status_code == 422


@pytest.mark.parametrize("bad_date", ["not-a-date", "2026-13-40", "21.6.2026", ""])
def test_invalid_date_rejected(bad_date):
    r = client.post("/api/analyze", json={"lat": 50.0, "lon": 14.0, "date": bad_date, "timezone": "UTC"})
    assert r.status_code == 422


def test_invalid_timezone_rejected():
    r = client.post("/api/analyze", json={"lat": 50.0, "lon": 14.0, "date": "2026-06-21", "timezone": "Mars/Phobos"})
    assert r.status_code == 422


def test_missing_fields_rejected():
    r = client.post("/api/analyze", json={"lat": 50.0})
    assert r.status_code == 422


def test_valid_timezone_names_accepted(monkeypatch):
    _patch_analyze(monkeypatch, lambda lat, lon, when: _fake_analysis())
    for tz in ("UTC", "Europe/Prague", "America/New_York"):
        r = client.post("/api/analyze", json={"lat": 50.0, "lon": 14.0, "date": "2026-06-21", "timezone": tz})
        assert r.status_code == 200, f"timezone {tz} selhalo: {r.text}"


# --- Úspěšná odpověď - serializace, žádné přepočty ve web vrstvě ---


def test_valid_request_returns_serialized_analysis(monkeypatch):
    _patch_analyze(monkeypatch, lambda lat, lon, when: _fake_analysis())
    r = client.post("/api/analyze", json={"lat": 50.5552, "lon": 13.9312, "date": "2026-06-21", "timezone": "Europe/Prague"})
    assert r.status_code == 200
    data = r.json()
    assert data["location"]["latitude"] == 50.5552
    assert data["dem"]["provider"] == "cuzk_dmr5g"
    assert len(data["terrain"]["azimuths_deg"]) == 360
    assert data["visibility"]["terrain_obstruction"] is False


def test_analyze_endpoint_includes_full_sunset_score(monkeypatch):
    _patch_analyze(monkeypatch, lambda lat, lon, when: _fake_analysis())
    r = client.post("/api/analyze", json={"lat": 50.5552, "lon": 13.9312, "date": "2026-06-21", "timezone": "Europe/Prague"})
    data = r.json()
    assert "sunset_score" in data
    assert 0 <= data["sunset_score"]["overall_0_100"] <= 100
    assert len(data["sunset_score"]["components"]) == 6
    assert data["rating"]["overall_0_100"] == data["sunset_score"]["rating"]["overall_0_100"]  # terrain-only zůstal beze změny


def test_analyze_endpoint_flags_reduced_dem_quality(monkeypatch):
    # Milestone "Trustworthy Results with Partial Cache" - stejné
    # odvozené pole jako /api/find_sunset a /api/search
    # (_dem_quality_fields()), tady počítané přímo v _serialize_analysis().
    analysis = _fake_analysis()
    analysis.dem.resolution_m = 36.1
    _patch_analyze(monkeypatch, lambda lat, lon, when: analysis)
    r = client.post("/api/analyze", json={"lat": 50.5552, "lon": 13.9312, "date": "2026-06-21", "timezone": "Europe/Prague"})
    data = r.json()
    assert data["dem"]["reduced_dem_quality"] is True


def test_analyze_endpoint_includes_general_sky_and_horizon_view(monkeypatch):
    # Milník "Postupné vylepšení" (Fáze 2) - "obecná předpověď", nezávislá
    # na terénu KONKRÉTNÍHO bodu analyzovaného v téhle odpovědi. Fixture
    # `_no_real_weather_network_calls` vrací low_cloud=5% -> "likely"
    # přímý výhled a "mixed" oblohu (viz tests/test_sunset_score.py pro
    # izolované testy samotné klasifikace).
    _patch_analyze(monkeypatch, lambda lat, lon, when: _fake_analysis())
    r = client.post("/api/analyze", json={"lat": 50.5552, "lon": 13.9312, "date": "2026-06-21", "timezone": "Europe/Prague"})
    data = r.json()
    assert data["general_sky"]["key"] is not None
    assert data["general_horizon_view"]["key"] == "likely"
    assert data["general_horizon_view"]["low_cloud_pct"] == 5.0
    # obě pole existují NEZÁVISLE vedle sebe v odpovědi, ne jako alias
    assert "sunset_type" in data["sunset_score"]


def test_analyze_endpoint_general_sky_honestly_null_without_weather(monkeypatch):
    # Zadání "nevymýšlej chybějící data" - bez počasí musí `general_sky`/
    # `general_horizon_view` být poctivě `null`, ne nějaký fake default.
    from app.weather_provider import WeatherUnavailableError

    def failing_fetch(*a, **kw):
        raise WeatherUnavailableError("network down")

    monkeypatch.setattr("app.main.weather_cache.fetch_weather_cached", failing_fetch)
    _patch_analyze(monkeypatch, lambda lat, lon, when: _fake_analysis())
    r = client.post("/api/analyze", json={"lat": 50.5552, "lon": 13.9312, "date": "2026-06-21", "timezone": "Europe/Prague"})
    data = r.json()
    assert data["general_sky"]["key"] is None
    assert data["general_horizon_view"]["key"] is None
    assert data["general_horizon_view"]["low_cloud_pct"] is None


def test_analyze_endpoint_includes_color_and_inversion_potential(monkeypatch):
    # Milník 12 (zadání bod 2/3) - `color_potential`/`inversion_potential`
    # (pro TENTO analyzovaný bod) i `general_color_potential`/
    # `general_inversion_potential` (nezávislé na konkrétním vybraném
    # bodu) musí být v odpovědi vždy přítomné vedle `general_sky`.
    _patch_analyze(monkeypatch, lambda lat, lon, when: _fake_analysis())
    r = client.post("/api/analyze", json={"lat": 50.5552, "lon": 13.9312, "date": "2026-06-21", "timezone": "Europe/Prague"})
    data = r.json()
    assert data["color_potential"]["key"] is not None
    assert data["inversion_potential"]["key"] is not None
    assert data["general_color_potential"]["key"] == data["color_potential"]["key"]
    assert data["general_inversion_potential"]["key"] is not None


def test_analyze_endpoint_general_inversion_potential_independent_of_point_elevation(monkeypatch):
    # `_fake_analysis()` dává nízkou elevaci (viz fixture) - obecná
    # inverze přesto nesmí být prostě "unavailable"/tichá kopie
    # specifické hodnoty jen proto, že vybraný bod je nížinný. Fixture
    # weather má low_cloud=5% (žádný mlžný signál), takže obě hodnoty tu
    # vyjdou "unlikely" - skutečný rozdíl ověřují izolované testy v
    # tests/test_sunset_score.py; tenhle test jen ověřuje, že pole
    # existuje a je poctivě vyplněné (ne None) i bez preferencí.
    _patch_analyze(monkeypatch, lambda lat, lon, when: _fake_analysis())
    r = client.post("/api/analyze", json={"lat": 50.5552, "lon": 13.9312, "date": "2026-06-21", "timezone": "Europe/Prague"})
    data = r.json()
    assert data["general_inversion_potential"]["key"] == "unlikely"


def test_analyze_endpoint_color_and_inversion_potential_honestly_null_without_weather(monkeypatch):
    from app.weather_provider import WeatherUnavailableError

    def failing_fetch(*a, **kw):
        raise WeatherUnavailableError("network down")

    monkeypatch.setattr("app.main.weather_cache.fetch_weather_cached", failing_fetch)
    _patch_analyze(monkeypatch, lambda lat, lon, when: _fake_analysis())
    r = client.post("/api/analyze", json={"lat": 50.5552, "lon": 13.9312, "date": "2026-06-21", "timezone": "Europe/Prague"})
    data = r.json()
    assert data["color_potential"]["key"] is None
    assert data["inversion_potential"]["key"] is None
    assert data["general_color_potential"]["key"] is None
    assert data["general_inversion_potential"]["key"] is None


def test_analyze_endpoint_full_resolution_not_flagged(monkeypatch):
    _patch_analyze(monkeypatch, lambda lat, lon, when: _fake_analysis())  # resolution_m=10.0
    r = client.post("/api/analyze", json={"lat": 50.5552, "lon": 13.9312, "date": "2026-06-21", "timezone": "Europe/Prague"})
    data = r.json()
    assert data["dem"]["reduced_dem_quality"] is False


def test_analyze_endpoint_with_preferences_includes_user_match(monkeypatch):
    _patch_analyze(monkeypatch, lambda lat, lon, when: _fake_analysis())
    r = client.post("/api/analyze", json={
        "lat": 50.5552, "lon": 13.9312, "date": "2026-06-21", "timezone": "Europe/Prague",
        "preferences": {"photography": True}, "distance_km": 5.0,
    })
    data = r.json()
    assert "user_match" in data
    assert data["user_match"]["match_pct"] is not None


def test_analyze_endpoint_without_preferences_omits_user_match(monkeypatch):
    _patch_analyze(monkeypatch, lambda lat, lon, when: _fake_analysis())
    r = client.post("/api/analyze", json={"lat": 50.5552, "lon": 13.9312, "date": "2026-06-21", "timezone": "Europe/Prague"})
    data = r.json()
    assert "user_match" not in data


def test_environment_signal_for_point_uses_national_grid_not_just_old_regions(monkeypatch):
    # Milestone "Data Foundation" - Milešovka (50.5552, 13.9312) NENÍ
    # uvnitř žádného ze 3 starých regionů (beskydy/krkonose/vsetinsko),
    # ale JE uvnitř nějaké dlaždice celostátní mřížky - regresní test na
    # přesně tenhle rozdíl v chování.
    from app.environment_context import EnvironmentFeatures

    calls = []

    def fake_discover(region, bbox):
        calls.append(region)
        return EnvironmentFeatures(water_points=[(50.5552, 13.9312)], forest_points=[], city_points=[])

    monkeypatch.setattr(main_module, "discover_environment_features_cached", fake_discover)
    near_water, near_forest, near_city = main_module._environment_signal_for_point(50.5552, 13.9312)
    assert near_water is True
    assert len(calls) == 1
    assert calls[0].startswith("grid_")


def test_environment_signal_for_point_degrades_gracefully_on_fetch_failure(monkeypatch):
    def fake_discover(region, bbox):
        raise RuntimeError("network down")

    monkeypatch.setattr(main_module, "discover_environment_features_cached", fake_discover)
    result = main_module._environment_signal_for_point(50.5552, 13.9312)
    assert result == (None, None, None)  # nikdy nespadne request kvůli tomuhle


def test_find_sunset_result_includes_weather_and_sunset_type_and_score(fake_catalog, fake_cache, fake_worker):
    cache_state, static_by_id = fake_cache
    cache_state["near"] = "ready"
    static_by_id["near"] = _fake_static_terrain(50.0, 14.0, elevation_m=1300.0)
    r = client.post("/api/find_sunset", json={"lat": 50.0, "lon": 14.0, "radius_km": 10})
    best = r.json()["results"][0]
    assert best["id"] == "near"
    assert 0 <= best["sunset_score"] <= 100
    assert best["weather"] is not None
    assert best["weather"]["cloud_cover_pct"] == 20.0
    assert best["sunset_type"]["key"] is not None
    assert best["hike_access_estimate"] in ("easy", "moderate", "hike", "adventure")
    assert len(best["score_components"]) == 6


def test_find_sunset_weather_failure_degrades_gracefully(fake_catalog, fake_cache, fake_worker, monkeypatch):
    cache_state, static_by_id = fake_cache
    cache_state["near"] = "ready"
    static_by_id["near"] = _fake_static_terrain(50.0, 14.0, elevation_m=1300.0)

    from app.weather_provider import WeatherUnavailableError

    def failing_fetch(*a, **kw):
        raise WeatherUnavailableError("network down")

    monkeypatch.setattr(main_module.weather_cache, "fetch_weather_cached", failing_fetch)
    r = client.post("/api/find_sunset", json={"lat": 50.0, "lon": 14.0, "radius_km": 10})
    assert r.status_code == 200
    best = r.json()["results"][0]
    assert best["weather"] is None
    assert best["sunset_score"] > 0  # terrain + sun_position pořád dají smysluplné skóre
    assert best["sunset_type"]["key"] is None  # bez počasí žádný badge


def test_find_sunset_with_preferences_reorders_by_combined_match(fake_catalog, fake_cache, fake_worker):
    cache_state, static_by_id = fake_cache
    # "near" a "mid" mají stejný plochý terén -> podobný fyzikální sunset_score,
    # ale "near" je peak na vysoké elevaci (mountain match), "mid" je viewpoint nízko.
    cache_state["near"] = "ready"
    static_by_id["near"] = _fake_static_terrain(50.0, 14.0, elevation_m=1300.0)
    cache_state["mid"] = "ready"
    static_by_id["mid"] = _fake_static_terrain(50.05, 14.0, elevation_m=1300.0)

    r = client.post("/api/find_sunset", json={
        "lat": 50.0, "lon": 14.0, "radius_km": 10, "preferences": {"scene_types": ["mountain"]},
    })
    data = r.json()
    near_result = next(x for x in data["results"] if x["id"] == "near")
    assert near_result["user_match"] is not None
    assert near_result["user_match"]["match_pct"] == 100  # "near" je peak (mountain-preferenci sedí)


def test_find_sunset_results_include_color_and_inversion_potential(fake_catalog, fake_cache, fake_worker):
    cache_state, static_by_id = fake_cache
    cache_state["near"] = "ready"
    static_by_id["near"] = _fake_static_terrain(50.0, 14.0, elevation_m=1300.0)
    r = client.post("/api/find_sunset", json={"lat": 50.0, "lon": 14.0, "radius_km": 10})
    data = r.json()
    near_result = next(x for x in data["results"] if x["id"] == "near")
    # Milník 12 (zadání bod 2/3) - stejná fixní autouse weather (low_cloud
    # 5%) jako `/api/analyze` testy výše -> deterministický "warm"/"unlikely".
    assert near_result["color_potential"]["key"] == "warm"
    assert near_result["inversion_potential"]["key"] == "unlikely"


def test_find_sunset_with_vivid_colors_preference_reorders_by_color_match(fake_catalog, fake_cache, fake_worker, monkeypatch):
    # Autouse weather fixture dává stejné počasí všem kandidátům (žádný
    # reálný rozdíl v barevném potenciálu mezi "near"/"mid" tady), takže
    # test monkeypatchuje `classify_color_potential` přímo - ověřuje jen
    # ŽE je nová preference skutečně PROPOJENÁ s `/api/find_sunset`
    # pořadím (chování samotné funkce mají izolované testy v
    # tests/test_sunset_score.py/test_scoring.py).
    from app.sunset_score import ColorPotentialResult

    cache_state, static_by_id = fake_cache
    cache_state["near"] = "ready"
    static_by_id["near"] = _fake_static_terrain(50.0, 14.0, elevation_m=1300.0)
    cache_state["mid"] = "ready"
    static_by_id["mid"] = _fake_static_terrain(50.05, 14.0, elevation_m=1300.0)

    def fake_color_potential(weather):
        # "near" dostane "vivid", "mid" "muted" - rozlišeno podle
        # latitude, protože obě candidate_type mají jinak stejnou fake
        # weather (autouse fixture výše).
        return ColorPotentialResult("vivid", "Potential for a red-to-pink sky", "test")

    monkeypatch.setattr(main_module, "classify_color_potential", fake_color_potential)
    r = client.post("/api/find_sunset", json={
        "lat": 50.0, "lon": 14.0, "radius_km": 10, "preferences": {"vivid_colors": True},
    })
    data = r.json()
    near_result = next(x for x in data["results"] if x["id"] == "near")
    assert near_result["user_match"]["match_pct"] == 100  # "vivid" sedí na 100 %


def test_analyze_location_called_with_correct_arguments(monkeypatch):
    captured = {}

    def fake_analyze(lat, lon, when):
        captured["lat"] = lat
        captured["lon"] = lon
        captured["when"] = when
        return _fake_analysis()

    _patch_analyze(monkeypatch, fake_analyze)
    client.post("/api/analyze", json={"lat": 50.5552, "lon": 13.9312, "date": "2026-06-21", "timezone": "Europe/Prague"})

    assert captured["lat"] == 50.5552
    assert captured["lon"] == 13.9312
    assert captured["when"].tzinfo is not None  # web vrstva NIKDY nesmí poslat naivní datetime
    assert captured["when"].year == 2026 and captured["when"].month == 6 and captured["when"].day == 21


def test_nan_values_serialized_as_null_not_invalid_json(monkeypatch):
    def fake_analyze(lat, lon, when):
        result = _fake_analysis()
        result.terrain.minimum_horizon_deg = float("nan")
        return result

    _patch_analyze(monkeypatch, fake_analyze)
    r = client.post("/api/analyze", json={"lat": 50.0, "lon": 14.0, "date": "2026-06-21", "timezone": "UTC"})
    assert r.status_code == 200
    assert r.json()["terrain"]["minimum_horizon_deg"] is None


def test_internal_profile_field_not_leaked_to_json(monkeypatch):
    """`TerrainInfo._profile` je interní pomůcka pro horizon_at() - nesmí
    skončit v JSON odpovědi (obsahuje numpy pole, není JSON-serializovatelné)."""
    _patch_analyze(monkeypatch, lambda lat, lon, when: _fake_analysis())
    r = client.post("/api/analyze", json={"lat": 50.0, "lon": 14.0, "date": "2026-06-21", "timezone": "UTC"})
    assert "_profile" not in r.json()["terrain"]


# --- Chybové stavy DEM (zadání bod 10/19) ---


def test_out_of_bounds_dem_returns_clear_error(monkeypatch):
    def fake_analyze(lat, lon, when):
        raise OutOfBoundsError("bod je mimo dlaždici")

    _patch_analyze(monkeypatch, fake_analyze)
    r = client.post("/api/analyze", json={"lat": 50.0, "lon": 14.0, "date": "2026-06-21", "timezone": "UTC"})
    assert r.status_code == 422
    assert "error" in r.json()


def test_outside_cuzk_coverage_returns_clear_error(monkeypatch):
    def fake_analyze(lat, lon, when):
        raise OutsideCoverageError("mimo ČR")

    _patch_analyze(monkeypatch, fake_analyze)
    r = client.post("/api/analyze", json={"lat": 48.0, "lon": 2.0, "date": "2026-06-21", "timezone": "UTC"})
    assert r.status_code == 422


def test_nodata_at_final_dem_source_returns_clear_error_not_500(monkeypatch):
    def fake_analyze(lat, lon, when):
        raise RuntimeError("NODATA na pozici pozorovatele")

    _patch_analyze(monkeypatch, fake_analyze)
    r = client.post("/api/analyze", json={"lat": 0.0, "lon": 0.0, "date": "2026-06-21", "timezone": "UTC"})
    assert r.status_code == 502
    assert "error" in r.json()


def test_dem_network_failure_returns_clear_error_not_raw_500(monkeypatch):
    """AUDIT NÁLEZ (Fáze 5): bod bez DEM pokrytí (např. oceán) dřív
    způsobil neošetřenou `requests.exceptions.HTTPError` a syrové HTTP 500
    bez jakékoliv zprávy uživateli - viz PLAN.md. Regresní test."""

    def fake_analyze(lat, lon, when):
        raise requests.exceptions.HTTPError("404 Client Error: Not Found for url: ...")

    _patch_analyze(monkeypatch, fake_analyze)
    r = client.post("/api/analyze", json={"lat": 0.0, "lon": -140.0, "date": "2026-06-21", "timezone": "UTC"})
    assert r.status_code == 502
    assert "error" in r.json()
    assert "500" not in str(r.status_code)  # explicitně ne syrová 500


def test_dem_network_failure_message_does_not_leak_raw_exception(monkeypatch):
    """AUDIT NÁLEZ (validační fáze): chybová hláška pro uživatele dřív
    obsahovala interpolovanou syrovou `requests` výjimku, včetně interní
    S3 URL DEM zdroje (`{exc}` přímo v textu). Uživatel musí dostat jen
    lidsky čitelnou zprávu bez technických detailů/URL - ty patří jen do
    server logu (viz app/main.py `logger.warning(...)`)."""

    def fake_analyze(lat, lon, when):
        raise requests.exceptions.HTTPError(
            "404 Client Error: Not Found for url: "
            "https://copernicus-dem-30m.s3.amazonaws.com/some/internal/path.tif"
        )

    _patch_analyze(monkeypatch, fake_analyze)
    r = client.post("/api/analyze", json={"lat": 0.0, "lon": -140.0, "date": "2026-06-21", "timezone": "UTC"})
    message = r.json()["error"]
    assert "amazonaws.com" not in message
    assert "404 Client Error" not in message
    assert "unavailable" in message.lower()


def test_dem_timeout_returns_clear_error(monkeypatch):
    def fake_analyze(lat, lon, when):
        raise requests.exceptions.Timeout("request timed out")

    _patch_analyze(monkeypatch, fake_analyze)
    r = client.post("/api/analyze", json={"lat": 50.0, "lon": 14.0, "date": "2026-06-21", "timezone": "UTC"})
    assert r.status_code == 502
    assert "error" in r.json()


def test_generic_value_error_returns_400(monkeypatch):
    def fake_analyze(lat, lon, when):
        raise ValueError("neco spatne s parametry")

    _patch_analyze(monkeypatch, fake_analyze)
    r = client.post("/api/analyze", json={"lat": 50.0, "lon": 14.0, "date": "2026-06-21", "timezone": "UTC"})
    assert r.status_code == 400


def test_fallback_info_visible_in_response(monkeypatch):
    def fake_analyze(lat, lon, when):
        result = _fake_analysis()
        result.dem.fallback_used = True
        result.dem.fallback_reason = "ČÚZK timeout"
        result.dem.provider = "copernicus_glo30"
        return result

    _patch_analyze(monkeypatch, fake_analyze)
    r = client.post("/api/analyze", json={"lat": 50.0, "lon": 14.0, "date": "2026-06-21", "timezone": "UTC"})
    data = r.json()
    assert data["dem"]["fallback_used"] is True
    assert data["dem"]["fallback_reason"] == "ČÚZK timeout"


def test_loading_indicator_css_does_not_override_hidden_attribute():
    """AUDIT NÁLEZ (validační fáze, nalezeno skutečným browser testem):
    `#loading { display: flex }` mělo vyšší CSS specificitu (ID selektor)
    než prohlížečovo výchozí `[hidden] { display: none }` (atributový
    selektor), takže loading indikátor byl VIDITELNÝ HNED PO NAČTENÍ
    STRÁNKY, i když měl `hidden` atribut. `TestClient` to nemůže odhalit
    (nevykresluje CSS) - tenhle test aspoň strukturálně hlídá, že rule
    zůstává naschválně scoped na `:not([hidden])`, aby regrese nebyla tichá."""
    css = pathlib.Path(__file__).resolve().parent.parent.joinpath("static", "style.css").read_text()
    css_no_comments = re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)
    match = re.search(r"#loading[^{]*\{[^}]*display\s*:\s*flex", css_no_comments)
    assert match is not None, "#loading display:flex rule not found in style.css"
    assert ":not([hidden])" in match.group(0), (
        "#loading's display:flex rule must be scoped with :not([hidden]), "
        "otherwise its ID-selector specificity silently overrides the "
        "browser's default [hidden] { display: none } rule"
    )


# --- Integrační testy (skutečný engine) ---


@pytest.mark.integration
def test_real_end_to_end_request():
    r = client.post("/api/analyze", json={"lat": 50.5552, "lon": 13.9312, "date": "2026-06-21", "timezone": "Europe/Prague"})
    assert r.status_code == 200
    data = r.json()
    assert data["dem"]["provider"] == "cuzk_dmr5g"
    assert len(data["terrain"]["azimuths_deg"]) == 360
    assert "astronomical_sunset" in data["solar"]


@pytest.mark.integration
def test_real_ocean_point_gives_clean_error_not_500():
    r = client.post("/api/analyze", json={"lat": 0.0, "lon": -140.0, "date": "2026-06-21", "timezone": "UTC"})
    assert r.status_code != 500
    assert r.status_code in (422, 502)
    assert "error" in r.json()


# --- Fáze 8 - GET /api/candidates (jen čte předpočítaný dataset, viz
# scripts/build_candidate_dataset.py a app/candidate_batch.py) ---


def test_candidates_endpoint_returns_precomputed_beskydy_dataset():
    r = client.get("/api/candidates?region=beskydy")
    assert r.status_code == 200
    data = r.json()
    assert data["region"] == "beskydy"
    assert data["candidate_count"] == len(data["candidates"])
    assert data["candidate_count"] > 0
    # deterministicky seřazeno sestupně podle ratingu (app.candidate_batch.rank_by_rating)
    ratings = [c["rating"] for c in data["candidates"]]
    assert ratings == sorted(ratings, reverse=True)


def test_candidates_endpoint_unknown_region_returns_404():
    r = client.get("/api/candidates?region=atlantida")
    assert r.status_code == 404
    assert "error" in r.json()


def test_candidates_endpoint_known_region_without_dataset_returns_404(monkeypatch, tmp_path):
    monkeypatch.setitem(main_module.CANDIDATE_DATASETS, "prazdna_oblast", tmp_path / "nonexistent.json")
    r = client.get("/api/candidates?region=prazdna_oblast")
    assert r.status_code == 404
    assert "error" in r.json()


# BUG NALEZENÝ (živé použití - "appka řekne cloudy, ale všechna místa v
# okolí ukazují ~90 %") - `/api/candidates` dřív vracelo JEN terrain-only
# `rating` z předpočítaného souboru, nikdy `sunset_score` (plný kompozit
# terén+počasí) - frontendová displayScore() tak VŽDY spadla na starý
# terrain-only fallback, i když dnešní počasí bylo úplně jiné. Testy níž
# ověřují opravu: `/api/candidates` teď pro kandidáty s cachovaným terénem
# dopočítá skutečný dnešní sunset_score, stejnou sdílenou funkcí jako
# `/api/find_sunset`/`/api/place`.


@pytest.fixture
def fake_candidates_dataset(monkeypatch, tmp_path):
    """Malý testovací `candidates_<region>.json` (přesně tvar, jaký
    vrací scripts/build_candidate_dataset.py) + fake `catalog_db.get_by_id`
    pro stejná ID - obojí musí mít SHODNÉ ID, jinak by se "živé"
    dopočítání nikdy nespustilo (přesně test pro fallback níž)."""
    dataset = {
        "region": "testregion",
        "generated_at": "2026-09-01T12:00:00+02:00",
        "analyzed_for_date": "2026-09-01",
        "candidate_count": 2,
        "candidates": [
            {
                "id": "cached", "name": "Cached Peak", "lat": 50.0, "lon": 14.0, "type": "peak",
                "osm_elevation_m": 900.0, "measured_elevation_m": 900.0, "rating": 97,
                "rating_label": "Excellent terrain visibility", "sunset_time": "2026-09-01T18:00:00+00:00",
                "sunset_azimuth_deg": 270.0, "sunset_terrain_horizon_deg": -1.0, "delay_minutes": 0.0,
                "terrain_obstruction": False,
            },
            {
                "id": "not-cached", "name": "Uncached Peak", "lat": 50.1, "lon": 14.1, "type": "peak",
                "osm_elevation_m": 800.0, "measured_elevation_m": 800.0, "rating": 90,
                "rating_label": "Excellent terrain visibility", "sunset_time": "2026-09-01T18:00:00+00:00",
                "sunset_azimuth_deg": 270.0, "sunset_terrain_horizon_deg": -1.0, "delay_minutes": 0.0,
                "terrain_obstruction": False,
            },
        ],
    }
    path = tmp_path / "candidates_testregion.json"
    path.write_text(json.dumps(dataset))
    monkeypatch.setitem(main_module.CANDIDATE_DATASETS, "testregion", path)

    catalog_row = {
        "id": "cached", "slug": "cached-peak", "name": "Cached Peak", "display_name": "Cached Peak",
        "candidate_type": "peak", "lat": 50.0, "lon": 14.0, "elevation_m": 900.0,
        "near_water": False, "near_forest": False, "near_city": False,
    }
    monkeypatch.setattr(main_module.catalog_db, "get_by_id", lambda cid, db_path=None: catalog_row if cid == "cached" else None)
    return dataset


def test_candidates_endpoint_attaches_live_sunset_score_when_terrain_cached(fake_candidates_dataset, fake_cache):
    cache_state, static_by_id = fake_cache
    cache_state["cached"] = "ready"
    static_by_id["cached"] = _fake_static_terrain(50.0, 14.0, elevation_m=900.0)

    r = client.get("/api/candidates?region=testregion")
    assert r.status_code == 200
    data = r.json()
    by_id = {c["id"]: c for c in data["candidates"]}

    live = by_id["cached"]
    assert live["rating"] == 97  # terrain-only pole beze změny (zpětná kompatibilita)
    assert live["sunset_score"] is not None  # NOVĚ doplněno
    assert live["sunset_score_label"]
    assert "color_potential" in live


def test_candidates_endpoint_falls_back_to_terrain_rating_when_terrain_not_cached(fake_candidates_dataset, fake_cache):
    # "not-cached" nemá terrain_cache záznam (fake_cache default = "missing")
    # ani catalog_db řádek (get_by_id vrací None) - appka NESMÍ spadnout
    # ani si vymyslet skóre, jen zůstane u terrain-only ratingu.
    r = client.get("/api/candidates?region=testregion")
    assert r.status_code == 200
    data = r.json()
    by_id = {c["id"]: c for c in data["candidates"]}

    stale = by_id["not-cached"]
    assert stale["rating"] == 90
    assert "sunset_score" not in stale  # žádné vymyšlené číslo


def test_candidates_endpoint_one_candidate_failing_does_not_break_others(fake_candidates_dataset, fake_cache, monkeypatch):
    cache_state, static_by_id = fake_cache
    cache_state["cached"] = "ready"
    static_by_id["cached"] = _fake_static_terrain(50.0, 14.0, elevation_m=900.0)

    def boom(*args, **kwargs):
        raise RuntimeError("simulovaná chyba dopočtu")

    monkeypatch.setattr(main_module, "_dynamic_result_from_cache", boom)
    r = client.get("/api/candidates?region=testregion")
    assert r.status_code == 200  # jeden selhavší kandidát nesmí shodit celý endpoint
    data = r.json()
    assert data["candidate_count"] == 2
    for c in data["candidates"]:
        assert "sunset_score" not in c  # ani jeden se nedočkal (mock vždy spadne), ale pořád jen fallback, ne chyba


def test_candidates_endpoint_excludes_candidate_below_terrain_quality_floor(monkeypatch, tmp_path):
    # Zpětná vazba - "proč bysme se měli zaobírat tím, kde ten západ
    # jde vidět jen trošku, když vedle je o dost lepší" - stejný práh
    # jako /api/find_sunset, aplikovaný na terrain-only `rating` z
    # předpočítaného souboru (nezávisle na tom, jestli se podařilo
    # dopočítat živé `sunset_score`).
    dataset = {
        "region": "lowfloor", "generated_at": "2026-09-01T12:00:00+02:00", "analyzed_for_date": "2026-09-01",
        "candidate_count": 2,
        "candidates": [
            {
                "id": "good", "name": "Good Peak", "lat": 50.0, "lon": 14.0, "type": "peak",
                "osm_elevation_m": 900.0, "measured_elevation_m": 900.0, "rating": 75,
                "rating_label": "Good terrain visibility", "sunset_time": "2026-09-01T18:00:00+00:00",
                "sunset_azimuth_deg": 270.0, "sunset_terrain_horizon_deg": -1.0, "delay_minutes": 0.0,
                "terrain_obstruction": False,
            },
            {
                "id": "bad", "name": "Blocked Hill", "lat": 50.1, "lon": 14.1, "type": "hill",
                "osm_elevation_m": 500.0, "measured_elevation_m": 500.0, "rating": 35,
                "rating_label": "Sunset largely obstructed", "sunset_time": "2026-09-01T18:00:00+00:00",
                "sunset_azimuth_deg": 270.0, "sunset_terrain_horizon_deg": 20.0, "delay_minutes": 60.0,
                "terrain_obstruction": True,
            },
        ],
    }
    path = tmp_path / "candidates_lowfloor.json"
    path.write_text(json.dumps(dataset))
    monkeypatch.setitem(main_module.CANDIDATE_DATASETS, "lowfloor", path)
    monkeypatch.setattr(main_module.catalog_db, "get_by_id", lambda cid, db_path=None: None)  # terén necachovaný - jen rating z filu rozhoduje

    r = client.get("/api/candidates?region=lowfloor")
    assert r.status_code == 200
    data = r.json()
    assert data["candidate_count"] == 1
    assert [c["id"] for c in data["candidates"]] == ["good"]


# --- Milestone "Nationwide Live Search" - GET /api/search, teď nad
# `catalog_db.radius_search()` (celostátní katalog), ne nad 3 ručně
# vybranými regiony. Stejné `fake_catalog`/`fake_cache`/`fake_worker`
# fixtures jako `/api/find_sunset` níže (žádná reálná SQLite/DEM/síť) -
# rating je teď dopočítaný ze (mockované) terrain cache, STEJNĚ jako
# u find_sunset, ne vyčtený z předpočítaného JSON pole. ---


def test_search_rated_candidates_sorted_by_rating_then_distance(fake_catalog, fake_cache, fake_worker):
    cache_state, static_by_id = fake_cache
    # Stejný plochý fake terén pro "near" (0km) a "mid" (~5.6km) ->
    # identický rating -> tie-break podle vzdálenosti (zadání bod "d-
    # eterministické řazení"), přesně jako u find_sunset níže.
    for cid, lat in (("near", 50.0), ("mid", 50.05)):
        cache_state[cid] = "ready"
        static_by_id[cid] = _fake_static_terrain(lat, 14.0, elevation_m=800.0)
    r = client.get("/api/search?lat=50.0&lon=14.0&radius_km=10")
    assert r.status_code == 200
    data = r.json()
    ids = [c["id"] for c in data["candidates"]]
    assert ids == ["near", "mid"]  # "far" (~222km) mimo radius, nikdy se nenačte
    assert data["rated_count"] == 2
    assert data["unrated_count"] == 0
    assert all(c["rating_status"] == "ready" and c["rating"] is not None for c in data["candidates"])


def test_search_unrated_candidates_come_after_rated_and_are_marked_honestly(fake_catalog, fake_cache, fake_worker):
    # zadání bod 3 - "nezpracované kandidáty transparentně odděl,
    # nikdy nevydávej za nejlepší místo": "mid" nemá hotový terén (cache
    # miss) - musí se objevit AŽ ZA ohodnoceným "near", s rating=None,
    # nikdy s fake/odhadnutým skóre, a musí se zařadit do workeru.
    cache_state, static_by_id = fake_cache
    cache_state["near"] = "ready"
    static_by_id["near"] = _fake_static_terrain(50.0, 14.0, elevation_m=900.0)
    # "mid" zůstává "missing" (výchozí fake_cache chování)

    r = client.get("/api/search?lat=50.0&lon=14.0&radius_km=10")
    assert r.status_code == 200
    data = r.json()
    ids = [c["id"] for c in data["candidates"]]
    assert ids == ["near", "mid"]  # ohodnocený vždy první, bez ohledu na budoucí rating
    assert data["rated_count"] == 1
    assert data["unrated_count"] == 1
    mid = next(c for c in data["candidates"] if c["id"] == "mid")
    assert mid["rating"] is None and mid["rating_status"] == "unrated"
    assert mid["sunset_time"] is None  # žádná vymyšlená dynamická data pro nezpracovaného kandidáta
    assert "mid" in fake_worker.enqueued_calls  # zařazeno k precomputu pro příští dotaz


def test_search_excludes_candidate_outside_radius(fake_catalog, fake_cache, fake_worker):
    r = client.get("/api/search?lat=50.0&lon=14.0&radius_km=10")
    ids = [c["id"] for c in r.json()["candidates"]]
    assert "far" not in ids  # ~222 km pryč


def test_search_boundary_distance_exactly_at_radius_is_included(fake_catalog, fake_cache, fake_worker):
    lat1, lon1 = 50.0, 14.0
    exact_km = haversine_m(lat1, lon1, 50.05, 14.0) / 1000.0  # přesná vzdálenost k "mid"
    r = client.get(f"/api/search?lat={lat1}&lon={lon1}&radius_km={exact_km}")
    assert r.status_code == 200
    ids = [c["id"] for c in r.json()["candidates"]]
    assert "mid" in ids


def test_search_distance_calculation_is_stable_across_repeated_calls(fake_catalog, fake_cache, fake_worker):
    distances = set()
    for _ in range(3):
        r = client.get("/api/search?lat=50.0&lon=14.0&radius_km=10")
        near = next(c for c in r.json()["candidates"] if c["id"] == "near")
        distances.add(near["distance_km"])
    assert distances == {0.0}


def test_search_no_candidates_in_radius_returns_empty_list_not_error(fake_catalog, fake_cache, fake_worker):
    r = client.get("/api/search?lat=0.0&lon=0.0&radius_km=5")
    assert r.status_code == 200
    data = r.json()
    assert data["candidate_count"] == 0
    assert data["candidates"] == []


def test_search_missing_location_returns_422(fake_catalog, fake_cache, fake_worker):
    r = client.get("/api/search")  # lat/lon jsou povinné query parametry
    assert r.status_code == 422


def test_search_invalid_coordinates_returns_400(fake_catalog, fake_cache, fake_worker):
    r = client.get("/api/search?lat=999&lon=14.0&radius_km=25")
    assert r.status_code == 400


def test_search_empty_catalog_returns_404(monkeypatch):
    monkeypatch.setattr(main_module.catalog_db, "count", lambda db_path=None: 0)
    r = client.get("/api/search?lat=50.0&lon=14.0&radius_km=25")
    assert r.status_code == 404


def test_search_radius_is_clamped_to_max(fake_catalog, fake_cache, fake_worker):
    r = client.get("/api/search?lat=50.0&lon=14.0&radius_km=99999")
    assert r.status_code == 200
    assert r.json()["radius_km"] == main_module.MAX_SEARCH_RADIUS_KM


def test_search_candidate_type_filter_excludes_other_types(fake_catalog, fake_cache, fake_worker):
    # "near" je "peak", "mid" je "viewpoint" (viz fake_catalog) - filtr
    # na "viewpoint" musí "near" vyřadit, i když je blíž.
    r = client.get("/api/search?lat=50.0&lon=14.0&radius_km=10&candidate_type=viewpoint")
    assert r.status_code == 200
    ids = [c["id"] for c in r.json()["candidates"]]
    assert ids == ["mid"]


# --- Milestone "Trustworthy Results with Partial Cache" - /api/search:
# DEM kvalita a odolnost proti chybě v dynamickém dopočtu. ---


def test_search_ready_candidate_includes_dem_quality_fields(fake_catalog, fake_cache, fake_worker):
    cache_state, static_by_id = fake_cache
    cache_state["near"] = "ready"
    static_by_id["near"] = _fake_static_terrain(50.0, 14.0)  # resolution_m=10.0 (viz _fake_static_terrain)
    r = client.get("/api/search?lat=50.0&lon=14.0&radius_km=1")
    data = r.json()
    near = next(c for c in data["candidates"] if c["id"] == "near")
    assert near["dem_resolution_m"] == 10.0
    assert near["dem_fallback_used"] is False
    assert near["reduced_dem_quality"] is False


def test_search_unrated_candidate_has_null_dem_quality_fields(fake_catalog, fake_cache, fake_worker):
    # "mid" zůstává "missing" (výchozí fake_cache) - žádná vymyšlená DEM
    # metadata pro kandidáta, který ještě nebyl analyzován.
    r = client.get("/api/search?lat=50.0&lon=14.0&radius_km=10")
    mid = next(c for c in r.json()["candidates"] if c["id"] == "mid")
    assert mid["dem_resolution_m"] is None
    assert mid["dem_fallback_used"] is None
    assert mid["reduced_dem_quality"] is None


def test_search_degraded_resolution_flagged(fake_catalog, fake_cache, fake_worker):
    cache_state, static_by_id = fake_cache
    cache_state["near"] = "ready"
    static = _fake_static_terrain(50.0, 14.0)
    static.dem.resolution_m = 36.1  # Milestone "DEM Quality and Resolution Control" - reálně pozorovaná degradace
    static_by_id["near"] = static
    r = client.get("/api/search?lat=50.0&lon=14.0&radius_km=1")
    near = next(c for c in r.json()["candidates"] if c["id"] == "near")
    assert near["dem_resolution_m"] == 36.1
    assert near["reduced_dem_quality"] is True


def test_search_dynamic_computation_failure_marked_error_not_crash(fake_catalog, fake_cache, fake_worker, monkeypatch):
    # Milestone "Trustworthy Results with Partial Cache" - dřív žádný
    # try/except kolem dynamického dopočtu v /api/search neexistoval -
    # jeden špatný kandidát by shodil CELÝ request na 500. Teď se
    # poctivě vykáže jako rating_status="error" (NE "ready", NE fake
    # skóre, NE "unrated" - to by znamenalo "ještě nezpracováno").
    cache_state, static_by_id = fake_cache
    cache_state["near"] = "ready"
    static_by_id["near"] = _fake_static_terrain(50.0, 14.0)
    cache_state["mid"] = "ready"
    static_by_id["mid"] = _fake_static_terrain(50.05, 14.0)

    real_fn = main_module.compute_dynamic_sunset

    def flaky(static, when, *a, **kw):
        if static.location.latitude == 50.0:
            raise RuntimeError("boom")
        return real_fn(static, when, *a, **kw)

    monkeypatch.setattr(main_module, "compute_dynamic_sunset", flaky)
    r = client.get("/api/search?lat=50.0&lon=14.0&radius_km=10")
    assert r.status_code == 200
    data = r.json()
    near = next(c for c in data["candidates"] if c["id"] == "near")
    assert near["rating_status"] == "error"
    assert near["rating"] is None  # žádné vymyšlené skóre
    assert near not in [c for c in data["candidates"] if c["rating_status"] == "ready"]
    assert data["rated_count"] == 1  # jen "mid" se skutečně počítá jako ohodnocené
    assert data["unrated_count"] == 1  # "near" (error) spočítané mezi neohodnocenými, ne zahozené


# --- Fáze 11 - GET /api/geocode (Nominatim, viz audit v app/main.py) ---
#
# Oprava produkčního nálezu (Render Free - 429 od Nominatim splácalo na
# matoucí 502, žádná cache) - skutečné volání Nominatim + cache + throttle
# teď žijí v app/geocode_cache.py (vlastní, podrobné testy v
# tests/test_geocode_cache.py). Testy tady ověřují ROUTE/HTTP vrstvu
# (stejný vzor jako `fake_cache`/`fake_worker` pro terrain cache výš) -
# `geocode_isolated_cache` přesměruje `geocode_cached()` na dočasnou
# SQLite v `tmp_path`, aby testy nesdílely stav mezi sebou ani s
# reálnou `data/geocode_cache.sqlite3`, ale pořád běžela SKUTEČNÁ
# cache/throttle logika, ne fake.


class _FakeGeocodeResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(f"{self.status_code} error")

    def json(self):
        return self._payload


@pytest.fixture
def geocode_isolated_cache(monkeypatch, tmp_path):
    db_path = tmp_path / "geocode_test.sqlite3"
    main_module.geocode_cache.init_db(db_path)
    original = main_module.geocode_cache.geocode_cached

    def wrapped(query, db_path=db_path):
        return original(query, db_path=db_path)

    monkeypatch.setattr(main_module.geocode_cache, "geocode_cached", wrapped)
    # Throttle je process-globální (zadání bod 2 - "max 1 req/s napříč
    # VŠEMI požadavky") - mezi testy by zbytečně zpomaloval, bez dopadu
    # na to, co se skutečně testuje.
    monkeypatch.setattr(main_module.geocode_cache, "_last_request_monotonic", 0.0)
    monkeypatch.setattr(main_module.geocode_cache, "MIN_REQUEST_INTERVAL_S", 0.0)
    return db_path


def test_geocode_returns_coordinates_for_known_place(monkeypatch, geocode_isolated_cache):
    def fake_get(url, params=None, headers=None, timeout=None):
        assert "countrycodes" in params and params["countrycodes"] == "cz"
        return _FakeGeocodeResponse(200, [{"lat": "49.4896099", "lon": "18.2651488", "display_name": "Pustevny, Česko"}])

    monkeypatch.setattr(main_module.geocode_cache.requests, "get", fake_get)
    r = client.get("/api/geocode?q=Pustevny")
    assert r.status_code == 200
    data = r.json()
    assert data["lat"] == pytest.approx(49.4896099)
    assert data["lon"] == pytest.approx(18.2651488)


def test_geocode_empty_query_returns_400():
    r = client.get("/api/geocode?q=")
    assert r.status_code == 400


def test_geocode_no_results_returns_404(monkeypatch, geocode_isolated_cache):
    def fake_get(url, params=None, headers=None, timeout=None):
        return _FakeGeocodeResponse(200, [])

    monkeypatch.setattr(main_module.geocode_cache.requests, "get", fake_get)
    r = client.get("/api/geocode?q=xxNoSuchPlacexx")
    assert r.status_code == 404


def test_geocode_network_error_returns_502(monkeypatch, geocode_isolated_cache):
    def fake_get(url, params=None, headers=None, timeout=None):
        raise requests.exceptions.ConnectionError("network down")

    monkeypatch.setattr(main_module.geocode_cache.requests, "get", fake_get)
    r = client.get("/api/geocode?q=Pustevny")
    assert r.status_code == 502


def test_geocode_rate_limited_by_nominatim_returns_503_not_502(monkeypatch, geocode_isolated_cache):
    # Přesně nahlášený produkční bug - Nominatim 429 dřív splácal na
    # stejnou matoucí 502 jako jakákoliv jiná chyba.
    def fake_get(url, params=None, headers=None, timeout=None):
        return _FakeGeocodeResponse(429, None)

    monkeypatch.setattr(main_module.geocode_cache.requests, "get", fake_get)
    r = client.get("/api/geocode?q=Brno")
    assert r.status_code == 503
    assert "429" not in r.json()["error"]  # uživateli srozumitelná zpráva, ne syrový kód


def test_geocode_repeated_identical_query_hits_cache_not_nominatim_again(monkeypatch, geocode_isolated_cache):
    # Zadání bod 3 - "opakované hledání stejného místa nesmí zbytečně
    # generovat další externí požadavky".
    call_count = {"n": 0}

    def fake_get(url, params=None, headers=None, timeout=None):
        call_count["n"] += 1
        return _FakeGeocodeResponse(200, [{"lat": "49.19", "lon": "16.61", "display_name": "Brno, Česko"}])

    monkeypatch.setattr(main_module.geocode_cache.requests, "get", fake_get)
    r1 = client.get("/api/geocode?q=Brno")
    r2 = client.get("/api/geocode?q=Brno")
    r3 = client.get("/api/geocode?q=  brno  ")  # normalizace (whitespace/case) sdílí stejný cache záznam
    assert r1.status_code == r2.status_code == r3.status_code == 200
    assert call_count["n"] == 1


# --- Milestone "Persistent Terrain Cache + Background Precomputation" -
# POST /api/find_sunset (PŘEPSÁNO - teď synchronní, cache-aware, viz
# app/main.py). `terrain_cache`/`get_worker()` jsou mockované (žádná
# reálná SQLite/DEM/síť) - tyhle testy ověřují ROUTING/pipeline (found
# vs. ready/enqueued/pending, prefilter strop, řazení, chybové stavy),
# ne znovu terrain engine (ten má vlastní testy v test_analyze_location.py
# a test_terrain_cache.py). `compute_dynamic_sunset()`/`compute_sunset_rating()`
# běží NAD SKUTEČNÝMI (ne mockovanými) drobnými objekty - je to čistá
# rychlá matematika bez I/O, takže se dá v testu spustit doopravdy a
# ověřit tak i reálnou integraci, ne jen že se něco zavolalo. ---

import numpy as np

from app.analyze_location import DemInfo as _DemInfo
from app.analyze_location import LocationInfo as _LocationInfo
from app.analyze_location import StaticTerrain as _StaticTerrain
from app.analyze_location import TerrainInfo as _TerrainInfo
from app.horizon import HorizonProfile as _HorizonProfile


def _fake_static_terrain(lat, lon, elevation_m=800.0):
    """Skutečný (ne mockovaný) `StaticTerrain` s plochým 360° horizontem
    (-1.0° všude) - `compute_dynamic_sunset()` nad ním doopravdy běží."""
    azimuths = np.arange(0.0, 360.0, 1.0)
    altitudes = np.full_like(azimuths, -1.0)
    profile = _HorizonProfile(azimuths_deg=azimuths, horizon_altitude_deg=altitudes)
    terrain = _TerrainInfo(
        azimuths_deg=[float(a) for a in azimuths],
        horizon_altitudes_deg=[float(v) for v in altitudes],
        minimum_horizon_deg=-1.0,
        maximum_horizon_deg=-1.0,
        slope_deg=5.0,
        aspect_deg=180.0,
        _profile=profile,
    )
    dem = _DemInfo(
        provider="cuzk_dmr5g", dataset="ČÚZK DMR 5G (DTM)", resolution_m=10.0, analysis_range_m=50000.0,
        source_crs="EPSG:4326", acquired_at=datetime(2026, 9, 25, tzinfo=timezone.utc), fallback_used=False,
        fallback_reason=None, quality_flag="ok",
    )
    location = _LocationInfo(latitude=lat, longitude=lon, elevation_m=elevation_m)
    return _StaticTerrain(location=location, dem=dem, terrain=terrain, warnings=[])


def _fake_static_terrain_blocked(lat, lon, elevation_m=800.0):
    """Stejné jako `_fake_static_terrain()`, ale se ZAKRYTÝM 360°
    horizontem (+20° všude) - simuluje místo se skutečně špatným
    terénem (zadání bod - terénní práh `MIN_DISPLAYED_TERRAIN_RATING`)."""
    azimuths = np.arange(0.0, 360.0, 1.0)
    altitudes = np.full_like(azimuths, 20.0)
    profile = _HorizonProfile(azimuths_deg=azimuths, horizon_altitude_deg=altitudes)
    terrain = _TerrainInfo(
        azimuths_deg=[float(a) for a in azimuths],
        horizon_altitudes_deg=[float(v) for v in altitudes],
        minimum_horizon_deg=20.0,
        maximum_horizon_deg=20.0,
        slope_deg=5.0,
        aspect_deg=180.0,
        _profile=profile,
    )
    dem = _DemInfo(
        provider="cuzk_dmr5g", dataset="ČÚZK DMR 5G (DTM)", resolution_m=10.0, analysis_range_m=50000.0,
        source_crs="EPSG:4326", acquired_at=datetime(2026, 9, 25, tzinfo=timezone.utc), fallback_used=False,
        fallback_reason=None, quality_flag="ok",
    )
    location = _LocationInfo(latitude=lat, longitude=lon, elevation_m=elevation_m)
    return _StaticTerrain(location=location, dem=dem, terrain=terrain, warnings=[])


class _FakeWorker:
    """Nahrazuje `app.terrain_worker.get_worker()` - žádné reálné
    vlákno/DEM volání, jen zaznamená, co by bylo zařazeno (zadání bod
    19 - "duplicate jobs" test využívá stejnou dedup logiku jako
    skutečný `TerrainWorker.enqueue()`)."""

    def __init__(self):
        self.enqueued_calls: list[str] = []
        self._pending: set[tuple[str, str]] = set()

    def enqueue(self, candidate_id, lat, lon, cache_key, region=None, candidate_type=None):
        key = (candidate_id, cache_key)
        if key in self._pending:
            return False
        self._pending.add(key)
        self.enqueued_calls.append(candidate_id)
        return True


@pytest.fixture
def fake_worker(monkeypatch):
    worker = _FakeWorker()
    monkeypatch.setattr(main_module, "get_worker", lambda: worker)
    return worker


@pytest.fixture
def fake_cache(monkeypatch):
    """`cache_state[(candidate_id)] = "ready"|"running"|"failed"` -
    neuvedené id = "missing" (výchozí chování `terrain_cache.get_status`).
    Weather je mockovaná globálně přes `_no_real_weather_network_calls`
    (autouse) výše, tady se řeší jen terrain cache."""
    cache_state: dict[str, str] = {}
    static_by_id: dict[str, object] = {}
    monkeypatch.setattr(main_module.terrain_cache, "compute_cache_key", lambda: "fake-cache-key")

    def fake_get_status(candidate_id, cache_key):
        return cache_state.get(candidate_id, "missing")

    def fake_get_static_terrain(candidate_id, cache_key):
        return static_by_id.get(candidate_id)

    monkeypatch.setattr(main_module.terrain_cache, "get_status", fake_get_status)
    monkeypatch.setattr(main_module.terrain_cache, "get_static_terrain", fake_get_static_terrain)
    return cache_state, static_by_id


def _fake_radius_search_over(catalog: list[dict]):
    """Milestone "Nationwide Live Search" - stejná geo-filtr/řazení
    logika jako `app.catalog_db.radius_search()` (bbox prefilter je ve
    skutečné implementaci jen výkonová optimalizace nad stejným
    haversine filtrem, takže testovací dvojník ji nepotřebuje
    opakovat), nad PEVNÝM testovacím katalogem místo SQLite - testy tak
    nezávisí na obsahu skutečného `data/national_catalog.sqlite3`
    (37k+ záznamů) ani na tom, odkud přesně čte produkční kód."""

    def fake_radius_search(lat, lon, radius_km, candidate_type=None, db_path=None):
        out = []
        for c in catalog:
            if candidate_type and c["candidate_type"] != candidate_type:
                continue
            dist_km = haversine_m(lat, lon, c["lat"], c["lon"]) / 1000.0
            if dist_km <= radius_km:
                out.append({**c, "distance_km": round(dist_km, 2)})
        out.sort(key=lambda c: c["distance_km"])
        return out

    return fake_radius_search


@pytest.fixture
def fake_catalog(monkeypatch, tmp_path):
    """Malý deterministický katalog místo skutečných ~37k kandidátů -
    testy nezávisí na obsahu reálného data/national_catalog.sqlite3.
    Patchuje OBA vstupní body, které produkční kód teď používá:
    `load_candidate_catalog()` (slug lookupy - `/sunset/{slug}/`,
    `/api/place/{slug}`) a `catalog_db.radius_search()` (geo search -
    `/api/search`, `/api/find_sunset`, Milestone "Nationwide Live
    Search") - oba musí vracet STEJNÉ kandidáty, jinak by si testy
    tiše odporovaly."""
    catalog = [
        {"id": "near", "slug": "near", "name": "Near", "display_name": "Near", "name_source": "osm", "lat": 50.0, "lon": 14.0, "candidate_type": "peak", "elevation_m": 900.0, "region": None, "near_water": None, "near_forest": None, "near_city": None, "source": "openstreetmap", "metadata": {}},
        {"id": "mid", "slug": "mid", "name": "Mid", "display_name": "Mid", "name_source": "osm", "lat": 50.05, "lon": 14.0, "candidate_type": "viewpoint", "elevation_m": 700.0, "region": None, "near_water": None, "near_forest": None, "near_city": None, "source": "openstreetmap", "metadata": {}},
        {"id": "far", "slug": "far", "name": "Far", "display_name": "Far", "name_source": "osm", "lat": 52.0, "lon": 14.0, "candidate_type": "tower", "elevation_m": 600.0, "region": None, "near_water": None, "near_forest": None, "near_city": None, "source": "openstreetmap", "metadata": {}},
    ]
    monkeypatch.setattr(main_module, "load_candidate_catalog", lambda: catalog)
    monkeypatch.setattr(main_module.catalog_db, "radius_search", _fake_radius_search_over(catalog))
    monkeypatch.setattr(main_module.catalog_db, "count", lambda db_path=None: len(catalog))
    return catalog


def test_find_sunset_reports_real_found_count_over_whole_catalog(fake_catalog, fake_cache, fake_worker):
    r = client.post("/api/find_sunset", json={"lat": 50.0, "lon": 14.0, "radius_km": 10})
    assert r.status_code == 200
    data = r.json()
    # "near" (0km) a "mid" (~5.5km) v radiusu, "far" (~222km) mimo
    assert data["found_count"] == 2
    assert data["to_analyze_count"] == 2


def test_find_sunset_no_candidates_in_radius_returns_empty_not_error(fake_catalog, fake_cache, fake_worker):
    r = client.post("/api/find_sunset", json={"lat": 0.0, "lon": 0.0, "radius_km": 5})
    assert r.status_code == 200
    data = r.json()
    assert data["found_count"] == 0
    assert data["results"] == []


def test_find_sunset_no_catalog_returns_404(monkeypatch):
    monkeypatch.setattr(main_module.catalog_db, "count", lambda db_path=None: 0)
    r = client.post("/api/find_sunset", json={"lat": 50.0, "lon": 14.0, "radius_km": 10})
    assert r.status_code == 404


def test_find_sunset_invalid_coordinates_rejected(fake_catalog, fake_cache, fake_worker):
    r = client.post("/api/find_sunset", json={"lat": 999, "lon": 14.0, "radius_km": 10})
    assert r.status_code == 422


def test_find_sunset_prefilter_caps_to_max_candidates(fake_catalog, fake_cache, fake_worker, monkeypatch):
    monkeypatch.setattr(main_module, "MAX_TERRAIN_ANALYSIS_CANDIDATES", 1)
    r = client.post("/api/find_sunset", json={"lat": 50.0, "lon": 14.0, "radius_km": 10})
    data = r.json()
    assert data["found_count"] == 2  # obě jsou skutečně v radiusu
    assert data["to_analyze_count"] == 1  # ale prefilter bere jen strop


def test_find_sunset_prefilter_picks_closest_first_not_by_type(fake_catalog, fake_cache, fake_worker, monkeypatch):
    # zadání bod J - prefilter nesmí zvýhodňovat typ (peak není lepší
    # než viewpoint) - "near" je peak, "mid" je viewpoint, ale "near"
    # je blíž, takže se má zpracovat jako první i při stropu 1.
    monkeypatch.setattr(main_module, "MAX_TERRAIN_ANALYSIS_CANDIDATES", 1)
    r = client.post("/api/find_sunset", json={"lat": 50.0, "lon": 14.0, "radius_km": 10})
    assert fake_worker.enqueued_calls == ["near"]


def test_find_sunset_cache_hit_returns_ready_result_without_enqueueing(fake_catalog, fake_cache, fake_worker):
    cache_state, static_by_id = fake_cache
    cache_state["near"] = "ready"
    static_by_id["near"] = _fake_static_terrain(50.0, 14.0, elevation_m=950.0)
    cache_state["mid"] = "ready"
    static_by_id["mid"] = _fake_static_terrain(50.05, 14.0, elevation_m=700.0)

    r = client.post("/api/find_sunset", json={"lat": 50.0, "lon": 14.0, "radius_km": 10})
    data = r.json()
    assert data["ready_count"] == 2
    assert data["enqueued_count"] == 0
    assert fake_worker.enqueued_calls == []
    best = data["results"][0]
    assert set(["id", "name", "lat", "lon", "type", "distance_km", "measured_elevation_m", "rating", "rating_label", "sunset_time", "sunset_azimuth_deg"]).issubset(best.keys())


def test_find_sunset_cache_miss_enqueues_and_returns_no_wait(fake_catalog, fake_cache, fake_worker):
    r = client.post("/api/find_sunset", json={"lat": 50.0, "lon": 14.0, "radius_km": 10})
    data = r.json()
    assert data["ready_count"] == 0
    assert data["enqueued_count"] == 2
    assert sorted(fake_worker.enqueued_calls) == ["mid", "near"]
    assert data["results"] == []  # zadání bod 13 - Best Sunset jen z ready, nikdy odhad


# --- Zpětná vazba (živé použití) - "proč bysme se měli zaobírat tím,
# kde ten západ jde vidět jen trošku, když vedle je o dost lepší" -
# MIN_DISPLAYED_TERRAIN_RATING terénní práh ---


def test_find_sunset_excludes_candidate_below_terrain_quality_floor(fake_catalog, fake_cache, fake_worker):
    cache_state, static_by_id = fake_cache
    cache_state["near"] = "ready"
    static_by_id["near"] = _fake_static_terrain(50.0, 14.0, elevation_m=950.0)  # dobrý terén
    cache_state["mid"] = "ready"
    static_by_id["mid"] = _fake_static_terrain_blocked(50.05, 14.0, elevation_m=700.0)  # zakrytý horizont

    r = client.post("/api/find_sunset", json={"lat": 50.0, "lon": 14.0, "radius_km": 10})
    data = r.json()
    # Oba kandidáti byli SKUTEČNĚ analyzovaní (ready_count počítá obojí),
    # ale "mid" se do doporučení nedostal - appka ho nikdy nevymyslí jako
    # "waiting"/"failed" (byl opravdu hotový), jen ho nenabídne.
    assert data["ready_count"] == 2
    assert data["below_quality_floor_count"] == 1
    result_ids = [r["id"] for r in data["results"]]
    assert result_ids == ["near"]
    assert all(c["id"] != "mid" for c in data["other_candidates"])


def test_find_sunset_all_candidates_below_floor_gives_empty_results_not_error(fake_catalog, fake_cache, fake_worker):
    cache_state, static_by_id = fake_cache
    cache_state["near"] = "ready"
    static_by_id["near"] = _fake_static_terrain_blocked(50.0, 14.0, elevation_m=950.0)
    cache_state["mid"] = "ready"
    static_by_id["mid"] = _fake_static_terrain_blocked(50.05, 14.0, elevation_m=700.0)

    r = client.post("/api/find_sunset", json={"lat": 50.0, "lon": 14.0, "radius_km": 10})
    assert r.status_code == 200
    data = r.json()
    assert data["ready_count"] == 2
    assert data["below_quality_floor_count"] == 2
    assert data["results"] == []


def test_find_sunset_running_candidate_is_pending_not_reenqueued(fake_catalog, fake_cache, fake_worker):
    cache_state, _ = fake_cache
    cache_state["near"] = "running"
    cache_state["mid"] = "missing"
    r = client.post("/api/find_sunset", json={"lat": 50.0, "lon": 14.0, "radius_km": 10})
    data = r.json()
    assert data["pending_count"] == 1  # "near" už běží, nesmí se zařadit znovu
    assert data["enqueued_count"] == 1  # jen "mid" je nový
    assert fake_worker.enqueued_calls == ["mid"]


def test_find_sunset_failed_cache_entry_is_retried_not_permanently_blacklisted(fake_catalog, fake_cache, fake_worker):
    cache_state, _ = fake_cache
    cache_state["near"] = "failed"
    cache_state["mid"] = "running"  # aby test izoloval jen chování "failed", ne "missing"
    r = client.post("/api/find_sunset", json={"lat": 50.0, "lon": 14.0, "radius_km": 10})
    assert fake_worker.enqueued_calls == ["near"]  # selhaný kandidát dostane novou šanci


def test_find_sunset_duplicate_enqueue_from_repeated_search_is_not_double_counted(fake_catalog, fake_cache, fake_worker):
    # Druhé volání /api/find_sunset dřív, než worker stihl cokoliv
    # zpracovat - fake_worker (stejná dedup logika jako skutečný
    # TerrainWorker.enqueue()) musí druhé zařazení odmítnout.
    client.post("/api/find_sunset", json={"lat": 50.0, "lon": 14.0, "radius_km": 10})
    r2 = client.post("/api/find_sunset", json={"lat": 50.0, "lon": 14.0, "radius_km": 10})
    data2 = r2.json()
    assert data2["enqueued_count"] == 0
    assert data2["pending_count"] == 2
    assert len(fake_worker.enqueued_calls) == 2  # ne 4 - druhé volání nic nepřidalo


def test_find_sunset_sorted_by_rating_desc_then_distance_then_name(fake_catalog, fake_cache, fake_worker):
    cache_state, static_by_id = fake_cache
    for cid, lat in (("near", 50.0), ("mid", 50.05)):
        cache_state[cid] = "ready"
        static_by_id[cid] = _fake_static_terrain(lat, 14.0, elevation_m=800.0)  # stejný plochý horizont -> stejný rating
    r = client.post("/api/find_sunset", json={"lat": 50.0, "lon": 14.0, "radius_km": 10})
    ids = [r_["id"] for r_ in r.json()["results"]]
    # shodný rating u obou (identický fake terén) -> tie-break podle vzdálenosti: "near" (0km) před "mid" (~5.5km)
    assert ids == ["near", "mid"]


def test_find_sunset_deterministic_ordering_across_repeated_runs(fake_catalog, fake_cache, fake_worker):
    cache_state, static_by_id = fake_cache
    for cid, lat in (("near", 50.0), ("mid", 50.05)):
        cache_state[cid] = "ready"
        static_by_id[cid] = _fake_static_terrain(lat, 14.0, elevation_m=800.0)
    orders = set()
    for _ in range(3):
        r = client.post("/api/find_sunset", json={"lat": 50.0, "lon": 14.0, "radius_km": 10})
        orders.add(tuple(r_["id"] for r_ in r.json()["results"]))
    assert len(orders) == 1


def test_find_sunset_dynamic_computation_failure_is_counted_not_fatal(fake_catalog, fake_cache, fake_worker, monkeypatch):
    cache_state, static_by_id = fake_cache
    cache_state["near"] = "ready"
    static_by_id["near"] = _fake_static_terrain(50.0, 14.0)
    cache_state["mid"] = "ready"
    static_by_id["mid"] = _fake_static_terrain(50.05, 14.0)

    real_fn = main_module.compute_dynamic_sunset

    def flaky(static, when, *a, **kw):
        if static.location.latitude == 50.0:
            raise RuntimeError("boom")
        return real_fn(static, when, *a, **kw)

    monkeypatch.setattr(main_module, "compute_dynamic_sunset", flaky)
    r = client.post("/api/find_sunset", json={"lat": 50.0, "lon": 14.0, "radius_km": 10})
    data = r.json()
    assert r.status_code == 200
    assert data["failed_count"] == 1
    assert data["ready_count"] == 1
    assert data["results"][0]["id"] == "mid"


# --- Milestone "Trustworthy Results with Partial Cache" - "fully_covered"
# (upřímnost "nejlepší v okolí" tvrzení) + DEM kvalita + odlišení
# "už dřív selhalo" od "nový pokus". ---


def test_find_sunset_fully_covered_true_when_whole_radius_is_ready(fake_catalog, fake_cache, fake_worker):
    cache_state, static_by_id = fake_cache
    for cid, lat in (("near", 50.0), ("mid", 50.05)):
        cache_state[cid] = "ready"
        static_by_id[cid] = _fake_static_terrain(lat, 14.0)
    r = client.post("/api/find_sunset", json={"lat": 50.0, "lon": 14.0, "radius_km": 10})
    data = r.json()
    assert data["fully_covered"] is True


def test_find_sunset_fully_covered_false_when_prefilter_truncates(fake_catalog, fake_cache, fake_worker, monkeypatch):
    # I když je ten jediný kontrolovaný kandidát "ready", `found_count`
    # (2) > `to_analyze_count` (1) - zbytek radiusu nebyl ani zkoušen,
    # takže "nejlepší v okolí" by bylo nepravdivé tvrzení.
    monkeypatch.setattr(main_module, "MAX_TERRAIN_ANALYSIS_CANDIDATES", 1)
    cache_state, static_by_id = fake_cache
    cache_state["near"] = "ready"
    static_by_id["near"] = _fake_static_terrain(50.0, 14.0)
    r = client.post("/api/find_sunset", json={"lat": 50.0, "lon": 14.0, "radius_km": 10})
    data = r.json()
    assert data["found_count"] == 2
    assert data["to_analyze_count"] == 1
    assert data["fully_covered"] is False


def test_find_sunset_fully_covered_false_when_anything_pending_or_enqueued(fake_catalog, fake_cache, fake_worker):
    cache_state, static_by_id = fake_cache
    cache_state["near"] = "ready"
    static_by_id["near"] = _fake_static_terrain(50.0, 14.0)
    # "mid" zůstává "missing" -> enqueued_count > 0
    r = client.post("/api/find_sunset", json={"lat": 50.0, "lon": 14.0, "radius_km": 10})
    data = r.json()
    assert data["enqueued_count"] == 1
    assert data["fully_covered"] is False


def test_find_sunset_previously_failed_tracked_separately_without_changing_retry(fake_catalog, fake_cache, fake_worker):
    # Milestone "Trustworthy Results with Partial Cache" - "failed" se
    # dřív počítalo stejně jako "missing" (jen "enqueued"/"pending"),
    # takže uživatel neviděl rozdíl mezi "nové místo" a "tohle už dřív
    # selhalo, zkouší se znovu". Retry chování (worker.enqueue se
    # zavolá úplně stejně) se NESMÍ změnit - jen přibyde viditelnost.
    cache_state, _ = fake_cache
    cache_state["near"] = "failed"
    cache_state["mid"] = "running"  # izolace - jen "near" je "failed"
    r = client.post("/api/find_sunset", json={"lat": 50.0, "lon": 14.0, "radius_km": 10})
    data = r.json()
    assert data["previously_failed_count"] == 1
    assert fake_worker.enqueued_calls == ["near"]  # retry beze změny (stejné jako před touhle milestone)
    assert data["enqueued_count"] == 1  # "near" se počítá v enqueued_count STEJNĚ jako dřív (žádná změna totalu)
    assert data["pending_count"] == 1  # "mid" (running)
    assert data["fully_covered"] is False


def test_find_sunset_results_include_dem_quality_fields(fake_catalog, fake_cache, fake_worker):
    cache_state, static_by_id = fake_cache
    cache_state["near"] = "ready"
    static = _fake_static_terrain(50.0, 14.0)
    static.dem.resolution_m = 36.1
    static_by_id["near"] = static
    r = client.post("/api/find_sunset", json={"lat": 50.0, "lon": 14.0, "radius_km": 1})
    result = r.json()["results"][0]
    assert result["dem_resolution_m"] == 36.1
    assert result["dem_fallback_used"] is False
    assert result["reduced_dem_quality"] is True


# --- Milestone "Correct Coverage Semantics and Candidate Accounting" -
# `fully_covered` dřív NEKONTROLOVALO `failed_count` (dynamický
# dopočet selhal u kandidáta se SKUTEČNĚ hotovým statickým terénem) -
# mohla tedy být nepravdivě `True`, i když relevantní kandidát v
# radiusu zůstal bez platného hodnocení. Testy níže ověřují opravenou
# definici a to, že čtyři počítadla (ready/failed/pending/enqueued)
# vždy přesně sečtou `to_analyze_count` - žádný kandidát nesmí
# "zmizet" z účetnictví. ---


def test_find_sunset_fully_covered_false_when_dynamic_computation_fails(fake_catalog, fake_cache, fake_worker, monkeypatch):
    # Regresní test přímo na opravenou chybu: oba kandidáti v radiusu
    # jsou "ready" (žádné odříznutí limitem, nic nečeká) - BEZ opravy
    # by `fully_covered` bylo `True`, i když "near" reálně nemá platné
    # hodnocení (dynamický dopočet spadl).
    cache_state, static_by_id = fake_cache
    cache_state["near"] = "ready"
    static_by_id["near"] = _fake_static_terrain(50.0, 14.0)
    cache_state["mid"] = "ready"
    static_by_id["mid"] = _fake_static_terrain(50.05, 14.0)

    real_fn = main_module.compute_dynamic_sunset

    def flaky(static, when, *a, **kw):
        if static.location.latitude == 50.0:
            raise RuntimeError("boom")
        return real_fn(static, when, *a, **kw)

    monkeypatch.setattr(main_module, "compute_dynamic_sunset", flaky)
    r = client.post("/api/find_sunset", json={"lat": 50.0, "lon": 14.0, "radius_km": 10})
    data = r.json()
    assert data["found_count"] == 2
    assert data["to_analyze_count"] == 2
    assert data["failed_count"] == 1
    assert data["pending_count"] == 0
    assert data["enqueued_count"] == 0
    assert data["fully_covered"] is False  # "mid" je ready, ale "near" selhalo - NENÍ to plné pokrytí


def test_find_sunset_fully_covered_can_be_false_while_a_valid_winner_exists(fake_catalog, fake_cache, fake_worker):
    # Zadání bod "Existuje platný vítěz, ale další relevantní
    # kandidáti ještě nebyli vyhodnoceni" - `results` NESMÍ být prázdné
    # jen proto, že pokrytí je částečné; `fully_covered` zůstává False.
    cache_state, static_by_id = fake_cache
    cache_state["near"] = "ready"
    static_by_id["near"] = _fake_static_terrain(50.0, 14.0)
    # "mid" zůstává "missing" -> čeká na vyhodnocení
    r = client.post("/api/find_sunset", json={"lat": 50.0, "lon": 14.0, "radius_km": 10})
    data = r.json()
    assert len(data["results"]) == 1
    assert data["results"][0]["id"] == "near"
    assert data["fully_covered"] is False


def test_find_sunset_empty_radius_fully_covered_true_and_counts_zero(fake_catalog, fake_cache, fake_worker):
    # Prázdný radius "o nic nepřišel" - vakuózně úplné pokrytí, a
    # všechna počítadla musí být poctivě nulová, ne jen chybějící.
    r = client.post("/api/find_sunset", json={"lat": 0.0, "lon": 0.0, "radius_km": 5})
    data = r.json()
    assert data["found_count"] == 0
    assert data["to_analyze_count"] == 0
    assert data["ready_count"] == 0
    assert data["enqueued_count"] == 0
    assert data["pending_count"] == 0
    assert data["failed_count"] == 0
    assert data["previously_failed_count"] == 0
    assert data["fully_covered"] is True
    assert data["results"] == []


def test_find_sunset_race_condition_missing_static_is_counted_not_dropped(fake_catalog, fake_cache, fake_worker):
    # Závod: `get_status()` řekne "ready", ale `get_static_terrain()`
    # mezitím vrátí None (smazáno/přepsáno jiným procesem) - dřív se
    # tenhle kandidát počítal do `enqueued_count` JEN když
    # `worker.enqueue()` vrátilo True, jinak zmizel z účetnictví úplně
    # (ani enqueued, ani pending). Teď musí vždy skončit v jednom z
    # obou - celkový součet musí sedět na `to_analyze_count`.
    cache_state, static_by_id = fake_cache
    cache_state["near"] = "ready"  # ale static_by_id["near"] se NEnastaví -> get_static_terrain() vrátí None
    r = client.post("/api/find_sunset", json={"lat": 50.0, "lon": 14.0, "radius_km": 1})
    data = r.json()
    assert data["to_analyze_count"] == 1
    assert data["ready_count"] + data["failed_count"] + data["pending_count"] + data["enqueued_count"] == 1
    assert data["enqueued_count"] == 1  # nový závod -> worker.enqueue() vrací True (nebyl dřív zařazen)
    assert fake_worker.enqueued_calls == ["near"]


@pytest.mark.parametrize(
    "near_status,mid_status,near_ready,mid_dynamic_fails",
    [
        ("ready", "ready", True, False),
        ("ready", "missing", True, False),
        ("running", "missing", False, False),
        ("failed", "running", False, False),
        ("ready", "ready", True, True),
    ],
)
def test_find_sunset_counters_always_sum_to_to_analyze_count(
    fake_catalog, fake_cache, fake_worker, monkeypatch, near_status, mid_status, near_ready, mid_dynamic_fails,
):
    # Obecná invariant: ready_count + failed_count + pending_count +
    # enqueued_count MUSÍ přesně sečíst to_analyze_count, za libovolné
    # kombinace stavů - žádný kandidát se nesmí "ztratit" ani započítat
    # dvakrát, ať je cache prázdná, částečně hotová, nebo s chybami.
    cache_state, static_by_id = fake_cache
    cache_state["near"] = near_status
    cache_state["mid"] = mid_status
    if near_ready:
        static_by_id["near"] = _fake_static_terrain(50.0, 14.0)
    if mid_status == "ready":
        static_by_id["mid"] = _fake_static_terrain(50.05, 14.0)

    if mid_dynamic_fails:
        real_fn = main_module.compute_dynamic_sunset

        def flaky(static, when, *a, **kw):
            if static.location.latitude == 50.05:
                raise RuntimeError("boom")
            return real_fn(static, when, *a, **kw)

        monkeypatch.setattr(main_module, "compute_dynamic_sunset", flaky)

    r = client.post("/api/find_sunset", json={"lat": 50.0, "lon": 14.0, "radius_km": 10})
    data = r.json()
    total = data["ready_count"] + data["failed_count"] + data["pending_count"] + data["enqueued_count"]
    assert total == data["to_analyze_count"] == 2


# --- Milestone "Visible Waiting Candidates" - reprodukce reálného
# nahlášeného scénáře (hledání "Brno", radius 5 km): `found_count` 64,
# `to_analyze_count` 15 (strop), ale naživo jen 1-2 kandidáti `ready`
# (TERRAIN_WORKER_CONCURRENCY=1, sekvenční zpracování). `results` dřív
# byl JEDINÝ zdroj dat pro frontend - ostatních ~62 nalezených
# kandidátů (čekající i nad stropem analýzy) se nedalo vůbec zobrazit.
# `other_candidates` tohle doplňuje s pravdivým stavem, beze změny
# `results`/`fully_covered`/stropu MAX_TERRAIN_ANALYSIS_CANDIDATES. ---


def _many_candidates_catalog(n: int):
    """`n` kandidátů rozprostřených tak, aby `distance_km` bylo striktně
    rostoucí a deterministické (0.001° ~ 0.11 km) - stejný duch jako
    Brno scénář (desítky kandidátů blízko sebe ve městě, v radiusu
    jednotek kilometrů, ne desítek)."""
    return [
        {
            "id": f"node/{i}", "slug": f"peak-{i}", "name": f"Peak {i}", "display_name": f"Peak {i}",
            "name_source": "osm", "lat": 50.0 + i * 0.001, "lon": 14.0, "candidate_type": "peak",
            "elevation_m": 500.0, "region": None, "near_water": None, "near_forest": None, "near_city": None,
            "source": "openstreetmap", "metadata": {},
        }
        for i in range(n)
    ]


@pytest.fixture
def many_candidates(monkeypatch):
    catalog = _many_candidates_catalog(64)  # reálně naměřeno u "Brno" @ 5 km
    monkeypatch.setattr(main_module.catalog_db, "radius_search", _fake_radius_search_over(catalog))
    monkeypatch.setattr(main_module.catalog_db, "count", lambda db_path=None: len(catalog))
    return catalog


def test_find_sunset_64_candidates_only_one_ready_others_visible_as_waiting(many_candidates, fake_cache, fake_worker):
    # Přesná reprodukce nahlášeného chování: 64 nalezeno, strop 15, ale
    # jen 1 skutečně "ready" (worker se k ostatním ještě nedostal).
    cache_state, static_by_id = fake_cache
    cache_state["node/0"] = "ready"
    static_by_id["node/0"] = _fake_static_terrain(50.0, 14.0)
    for i in range(1, 15):
        cache_state[f"node/{i}"] = "running"  # worker na nich právě pracuje (concurrency=1)
    # "node/15".."node/63" zůstávají "missing" (výchozí) - nad stropem analýzy

    r = client.post("/api/find_sunset", json={"lat": 50.0, "lon": 14.0, "radius_km": 10})
    data = r.json()

    assert data["found_count"] == 64
    assert data["to_analyze_count"] == 15
    assert data["ready_count"] == 1
    assert len(data["results"]) == 1  # beze změny - `results` je pořád jen skutečně ready

    # Klíčová oprava: ostatních 63 kandidátů se NEZTRATÍ - jsou v
    # `other_candidates` s pravdivým stavem, ne tiše zahozeni.
    assert len(data["other_candidates"]) == 63
    other_by_id = {c["id"]: c["status"] for c in data["other_candidates"]}
    assert all(other_by_id[f"node/{i}"] == "evaluating" for i in range(1, 15))  # running v to_check
    assert all(other_by_id[f"node/{i}"] == "not_analyzed" for i in range(15, 64))  # nad stropem
    assert data["fully_covered"] is False

    # Kandidáti nad stropem se NEZAŘADILI do workeru - strop zůstal
    # zachovaný, žádná zvýšená DEM zátěž.
    assert all(f"node/{i}" not in fake_worker.enqueued_calls for i in range(15, 64))
    # Ani žádný ze "running" (už se zpracovává jinde) se znovu nezařadil.
    assert all(f"node/{i}" not in fake_worker.enqueued_calls for i in range(1, 15))


def test_find_sunset_other_candidates_update_as_jobs_progress(many_candidates, fake_cache, fake_worker):
    # "Některé úlohy se dokončí, jiné zůstanou čekající, některé selžou" -
    # druhé volání (stejná query, jako by frontend obnovil výsledky) musí
    # ukázat AKTUALIZOVANÝ stav, ne zamrzlý snímek z prvního požadavku.
    cache_state, static_by_id = fake_cache
    for i in range(15):
        cache_state[f"node/{i}"] = "running"

    r1 = client.post("/api/find_sunset", json={"lat": 50.0, "lon": 14.0, "radius_km": 10})
    data1 = r1.json()
    assert data1["ready_count"] == 0
    assert all(c["status"] == "evaluating" for c in data1["other_candidates"] if int(c["id"].split("/")[1]) < 15)

    # Mezitím worker dokončí pár kandidátů (2 ready, 1 trvale selhal),
    # zbytek doběhne (running).
    cache_state["node/0"] = "ready"
    static_by_id["node/0"] = _fake_static_terrain(50.0, 14.0)
    cache_state["node/1"] = "ready"
    static_by_id["node/1"] = _fake_static_terrain(50.001, 14.0)
    cache_state["node/2"] = "failed"

    r2 = client.post("/api/find_sunset", json={"lat": 50.0, "lon": 14.0, "radius_km": 10})
    data2 = r2.json()
    assert data2["ready_count"] == 2
    assert {c["id"] for c in data2["results"]} == {"node/0", "node/1"}
    other2 = {c["id"]: c["status"] for c in data2["other_candidates"]}
    # Milestone "Immediate Full Results" - "failed" status se teď ukazuje
    # výslovně (dřív splynul s obecným "pending") - retry proběhl
    # automaticky beze změny chování, jen je teď viditelný důvod.
    assert other2["node/2"] == "failed"
    assert data2["previously_failed_count"] == 1
    assert all(other2[f"node/{i}"] == "evaluating" for i in range(3, 15))


def test_find_sunset_other_candidates_shows_failed_dynamic_computation(many_candidates, fake_cache, fake_worker, monkeypatch):
    cache_state, static_by_id = fake_cache
    cache_state["node/0"] = "ready"
    static_by_id["node/0"] = _fake_static_terrain(50.0, 14.0)

    real_fn = main_module.compute_dynamic_sunset

    def flaky(static, when, *a, **kw):
        if static.location.latitude == 50.0:
            raise RuntimeError("boom")
        return real_fn(static, when, *a, **kw)

    monkeypatch.setattr(main_module, "compute_dynamic_sunset", flaky)
    r = client.post("/api/find_sunset", json={"lat": 50.0, "lon": 14.0, "radius_km": 10})
    data = r.json()
    other = {c["id"]: c["status"] for c in data["other_candidates"]}
    assert other["node/0"] == "failed"
    assert data["ready_count"] == 0


def test_find_sunset_other_candidates_not_enqueued_beyond_cap(many_candidates, fake_cache, fake_worker):
    # Nad-stropoví kandidáti se nesmí objevit ve `worker.enqueued_calls`
    # - žádná nová DEM zátěž jen proto, že teď jsou vidět v odpovědi.
    r = client.post("/api/find_sunset", json={"lat": 50.0, "lon": 14.0, "radius_km": 10})
    data = r.json()
    assert len(fake_worker.enqueued_calls) == data["enqueued_count"] == 15  # jen `to_check`, ne všech 64
    assert sorted(fake_worker.enqueued_calls) == sorted(f"node/{i}" for i in range(15))


def test_find_sunset_other_candidates_empty_radius(fake_catalog, fake_cache, fake_worker):
    r = client.post("/api/find_sunset", json={"lat": 0.0, "lon": 0.0, "radius_km": 5})
    data = r.json()
    assert data["other_candidates"] == []
    assert data["other_candidates_truncated"] is False


def test_find_sunset_other_candidates_truncated_for_very_large_radius(monkeypatch, fake_worker):
    # `other_candidates` je (stejně jako `/api/search`) ohraničené na
    # `MAX_SEARCH_RESULTS` (200), aby payload zůstal rozumný i nad velmi
    # hustě pokrytou oblastí - `truncated` flag poctivě signalizuje, že
    # ne všichni nalezení kandidáti se dostali do odpovědi (byť
    # `found_count` samo o sobě zůstává úplné, zadání bod "neodvozuj
    # úplnost z počítadel popisujících jen vybraný vzorek").
    catalog = _many_candidates_catalog(250)  # nad MAX_SEARCH_RESULTS (200)
    monkeypatch.setattr(main_module.catalog_db, "radius_search", _fake_radius_search_over(catalog))
    monkeypatch.setattr(main_module.catalog_db, "count", lambda db_path=None: len(catalog))
    monkeypatch.setattr(main_module.terrain_cache, "get_status", lambda cid, key: "missing")

    r = client.post("/api/find_sunset", json={"lat": 50.0, "lon": 14.0, "radius_km": 30})
    data = r.json()
    assert data["found_count"] == 250
    assert data["to_analyze_count"] == 15
    # 15 (to_check, všechny "waiting") + 185 ("not_analyzed") == 200 == MAX_SEARCH_RESULTS
    assert len(data["other_candidates"]) == 200
    assert data["other_candidates_truncated"] is True
    # 250 - 15 (to_check) - 185 (vráceno jako "not_analyzed") == 50 zbývá
    assert data["other_candidates_remaining"] == 50
    assert data["other_candidates_next_offset"] == 185


def test_find_sunset_other_candidates_pagination_offset_continues_where_left_off(monkeypatch, fake_worker):
    # Milestone "Immediate Full Results" - "Zobrazit další" musí
    # skutečně dostránkovat ZBYTEK (ne znovu od začátku, ne duplicitně).
    catalog = _many_candidates_catalog(250)
    monkeypatch.setattr(main_module.catalog_db, "radius_search", _fake_radius_search_over(catalog))
    monkeypatch.setattr(main_module.catalog_db, "count", lambda db_path=None: len(catalog))
    monkeypatch.setattr(main_module.terrain_cache, "get_status", lambda cid, key: "missing")

    r1 = client.post("/api/find_sunset", json={"lat": 50.0, "lon": 14.0, "radius_km": 30})
    data1 = r1.json()
    next_offset = data1["other_candidates_next_offset"]

    r2 = client.post(
        "/api/find_sunset",
        json={"lat": 50.0, "lon": 14.0, "radius_km": 30, "other_candidates_offset": next_offset},
    )
    data2 = r2.json()
    assert data2["other_candidates_truncated"] is False
    assert data2["other_candidates_remaining"] == 0
    ids1 = {c["id"] for c in data1["other_candidates"] if c["status"] == "not_analyzed"}
    ids2 = {c["id"] for c in data2["other_candidates"] if c["status"] == "not_analyzed"}
    assert ids1.isdisjoint(ids2)  # žádná duplicita mezi "stránkami"
    # druhá "stránka" musí obsahovat přesně zbývajících 50 (250 - 15 to_check - 185 z první stránky)
    assert len(ids2) == 50


# ============================= Milestone "Complete Product Build"
# (pokračování) - place pages `/sunset/{slug}/` + `/api/place/{slug}` +
# `/explore` (zadání bod 2/8, MANDATORY). Stejné fixtures jako
# /api/find_sunset výše (fake_catalog/fake_cache/fake_worker) - žádná
# reálná síť/terrain/SQLite. ============================


def test_place_page_returns_404_for_unknown_slug(fake_catalog, fake_cache, fake_worker):
    r = client.get("/sunset/does-not-exist/")
    assert r.status_code == 404
    assert "does-not-exist" in r.text


def test_place_page_returns_200_for_known_slug(fake_catalog, fake_cache, fake_worker):
    r = client.get("/sunset/near/")
    assert r.status_code == 200
    assert "Near" in r.text


def test_place_page_shows_real_coordinates_with_copy_and_maps_link(fake_catalog, fake_cache, fake_worker):
    # Fáze 5 (GPS souřadnice + kopírování) - skutečné lat/lon z katalogu,
    # formát "49.123456, 16.123456" (6 des. míst), tlačítko kopírování a
    # odkaz do Google Maps se STEJNÝMI souřadnicemi, žádný odhad.
    r = client.get("/sunset/near/")
    assert r.status_code == 200
    assert "50.000000, 14.000000" in r.text
    assert 'id="place-copy-coords-btn"' in r.text
    assert 'id="place-coords-feedback"' in r.text
    assert "https://www.google.com/maps?q=50.000000,14.000000" in r.text


def test_place_page_prefers_display_name_over_raw_osm_name(fake_catalog, fake_cache, fake_worker, monkeypatch):
    # Milestone "Data Foundation" - celostátní katalog má kandidáty bez
    # OSM name; `display_name` (name-resolution pipeline) musí být to,
    # co se skutečně zobrazí, ne surové/prázdné `name`.
    catalog = fake_catalog
    catalog[0]["name"] = None
    catalog[0]["display_name"] = "Vyhlídka – Testov"
    r = client.get("/sunset/near/")
    assert r.status_code == 200
    assert "Vyhlídka – Testov" in r.text


def test_find_sunset_result_uses_display_name(fake_catalog, fake_cache, fake_worker):
    catalog = fake_catalog
    catalog[0]["name"] = None
    catalog[0]["display_name"] = "Vyhlídka – Testov"
    cache_state, static_by_id = fake_cache
    cache_state["near"] = "ready"
    static_by_id["near"] = _fake_static_terrain(50.0, 14.0)
    r = client.post("/api/find_sunset", json={"lat": 50.0, "lon": 14.0, "radius_km": 10})
    data = r.json()
    assert data["results"][0]["name"] == "Vyhlídka – Testov"


def test_api_place_unknown_slug_returns_404(fake_catalog, fake_cache, fake_worker):
    r = client.get("/api/place/does-not-exist")
    assert r.status_code == 404


def test_api_place_cache_miss_returns_pending_and_enqueues(fake_catalog, fake_cache, fake_worker):
    r = client.get("/api/place/near")
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "pending"
    assert fake_worker.enqueued_calls == ["near"]


def test_api_place_running_status_is_pending_without_new_enqueue(fake_catalog, fake_cache, fake_worker):
    cache_state, _ = fake_cache
    cache_state["near"] = "running"
    r = client.get("/api/place/near")
    data = r.json()
    assert data["status"] == "pending"


def test_api_place_ready_returns_tonight_and_best_days(fake_catalog, fake_cache, fake_worker, monkeypatch):
    cache_state, static_by_id = fake_cache
    cache_state["near"] = "ready"
    static_by_id["near"] = _fake_static_terrain(50.0, 14.0, elevation_m=900.0)

    monkeypatch.setattr(
        "app.best_days.weather_cache.fetch_weather_batch_cached",
        lambda lat, lon, target_times, timezone_name="UTC": {},
    )
    r = client.get("/api/place/near")
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "ready"
    assert data["candidate"]["slug"] == "near"
    assert data["tonight"]["sunset_score"] is not None
    assert "rating_detail" in data["tonight"]
    # Milestone "Trustworthy Results with Partial Cache" - DEM warning
    # (viz Milestone "DEM Quality and Resolution Control") se dřív
    # nikam z cache do /api/place neunikl.
    assert data["tonight"]["dem_resolution_m"] == 10.0
    assert data["tonight"]["reduced_dem_quality"] is False
    assert len(data["best_days"]) == main_module.DEFAULT_BEST_DAYS_COUNT
    dates = {d["date"] for d in data["best_days"]}
    assert len(dates) == main_module.DEFAULT_BEST_DAYS_COUNT  # 7 RŮZNÝCH dnů, ne stejný den zkopírovaný


def test_api_place_reduced_dem_quality_flagged_in_tonight(fake_catalog, fake_cache, fake_worker, monkeypatch):
    cache_state, static_by_id = fake_cache
    cache_state["near"] = "ready"
    static = _fake_static_terrain(50.0, 14.0, elevation_m=900.0)
    static.dem.resolution_m = 36.1
    static_by_id["near"] = static
    monkeypatch.setattr(
        "app.best_days.weather_cache.fetch_weather_batch_cached",
        lambda lat, lon, target_times, timezone_name="UTC": {},
    )
    r = client.get("/api/place/near")
    assert r.status_code == 200
    assert r.json()["tonight"]["reduced_dem_quality"] is True


def test_api_place_without_distance_km_leaves_access_estimate_unknown(fake_catalog, fake_cache, fake_worker, monkeypatch):
    cache_state, static_by_id = fake_cache
    cache_state["near"] = "ready"
    static_by_id["near"] = _fake_static_terrain(50.0, 14.0)
    monkeypatch.setattr(
        "app.best_days.weather_cache.fetch_weather_batch_cached",
        lambda lat, lon, target_times, timezone_name="UTC": {},
    )
    r = client.get("/api/place/near")
    data = r.json()
    assert data["tonight"]["hike_access_estimate"] is None  # poctivě "neznámé", ne vymyšlené číslo
    assert data["tonight"]["distance_km"] is None


def test_api_place_with_distance_km_gives_real_access_estimate(fake_catalog, fake_cache, fake_worker, monkeypatch):
    cache_state, static_by_id = fake_cache
    cache_state["near"] = "ready"
    # elevation_m=400.0 (pod ELEVATION_MODERATE_FLOOR_M) - testuje čistě
    # distance-based "easy" větev, ne elevation floor (ten má vlastní
    # test v test_scoring.py).
    static_by_id["near"] = _fake_static_terrain(50.0, 14.0, elevation_m=400.0)
    monkeypatch.setattr(
        "app.best_days.weather_cache.fetch_weather_batch_cached",
        lambda lat, lon, target_times, timezone_name="UTC": {},
    )
    r = client.get("/api/place/near?distance_km=2.0")
    data = r.json()
    assert data["tonight"]["hike_access_estimate"] == "easy"
    assert data["tonight"]["distance_km"] == 2.0


def test_api_place_high_elevation_never_shows_easy_access_for_short_distance(fake_catalog, fake_cache, fake_worker, monkeypatch):
    # BUG NALEZENÝ živě (Lysá hora) - regression test na úrovni API, ne
    # jen jednotkový test compute logiky.
    cache_state, static_by_id = fake_cache
    cache_state["near"] = "ready"
    static_by_id["near"] = _fake_static_terrain(50.0, 14.0, elevation_m=1324.0)
    monkeypatch.setattr(
        "app.best_days.weather_cache.fetch_weather_batch_cached",
        lambda lat, lon, target_times, timezone_name="UTC": {},
    )
    r = client.get("/api/place/near?distance_km=2.0")
    data = r.json()
    assert data["tonight"]["hike_access_estimate"] != "easy"


def test_explore_page_returns_200():
    r = client.get("/explore")
    assert r.status_code == 200
    assert "explore" in r.text.lower()


def test_healthz_returns_ok_without_touching_catalog_or_dem():
    # Příprava bezplatného testovacího nasazení (zadání bod 3) - health
    # check nesmí dotazovat katalog/terén/DEM, jen potvrdit, že proces žije.
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_api_analyze_rate_limiter_returns_429_after_limit_exceeded(monkeypatch):
    # Příprava bezplatného testovacího nasazení (zadání bod 5) - "veřejný
    # provoz nemůže spouštět neomezeně drahé výpočty". Mockuje
    # `analyze_location` pryč (rychlé, deterministické - testuje jen
    # limiter, ne znovu terrain engine).
    monkeypatch.setattr(main_module, "analyze_location", lambda lat, lon, when: _fake_analysis())
    main_module._analyze_limiter._hits.clear()
    body = {"lat": 50.0, "lon": 14.0, "date": "2026-06-21", "timezone": "Europe/Prague"}
    try:
        last = None
        for _ in range(main_module._analyze_limiter.max_requests):
            last = client.post("/api/analyze", json=body)
        assert last.status_code == 200
        blocked = client.post("/api/analyze", json=body)
        assert blocked.status_code == 429
    finally:
        main_module._analyze_limiter._hits.clear()


def test_api_analyze_rate_limiter_is_per_ip(monkeypatch):
    # Zadání bod 5 - limiter nesmí omylem sdílet stav mezi různými klienty
    # (jinak by jeden testerský IP mohl "ublokovat" ostatní).
    monkeypatch.setattr(main_module, "analyze_location", lambda lat, lon, when: _fake_analysis())
    main_module._analyze_limiter._hits.clear()
    body = {"lat": 50.0, "lon": 14.0, "date": "2026-06-21", "timezone": "Europe/Prague"}
    try:
        for _ in range(main_module._analyze_limiter.max_requests):
            client.post("/api/analyze", json=body, headers={"X-Forwarded-For": "1.1.1.1"})
        blocked = client.post("/api/analyze", json=body, headers={"X-Forwarded-For": "1.1.1.1"})
        assert blocked.status_code == 429
        other_ip = client.post("/api/analyze", json=body, headers={"X-Forwarded-For": "2.2.2.2"})
        assert other_ip.status_code == 200
    finally:
        main_module._analyze_limiter._hits.clear()
