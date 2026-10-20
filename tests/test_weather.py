"""Testy pro app/weather_provider.py + app/weather_cache.py (milestone
"Complete Product Build"). `requests.get` je mockovaný pro provider
testy (žádná reálná síť v unit testech) - živě ověřeno jednou ručně
při vývoji (viz PLAN.md), ne opakovaně v test suite."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
import requests

from app.weather_cache import fetch_weather_batch_cached, fetch_weather_cached, get_cached_weather, init_db, save_weather
from app.weather_provider import WeatherInfo, WeatherUnavailableError, fetch_weather_forecast, fetch_weather_forecast_batch


class _FakeResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(f"{self.status_code}")

    def json(self):
        return self._payload


def _fake_open_meteo_payload(hours: list[str]):
    n = len(hours)
    return {
        "hourly": {
            "time": hours,
            "cloud_cover": [20.0] * n,
            "cloud_cover_low": [5.0] * n,
            "cloud_cover_mid": [10.0] * n,
            "cloud_cover_high": [5.0] * n,
            "visibility": [35000.0] * n,
            "precipitation": [0.0] * n,
            "precipitation_probability": [10] * n,
            "temperature_2m": [15.0] * n,
            "wind_speed_10m": [8.0] * n,
            "wind_direction_10m": [270.0] * n,
            "relative_humidity_2m": [55.0] * n,
            "surface_pressure": [1013.0] * n,
            "weather_code": [1] * n,
        }
    }


def test_fetch_weather_forecast_finds_closest_hour(monkeypatch):
    # Datum musí být VŽDY "dnes/zítra" relativně ke skutečnému běhu testu
    # (ne natvrdo minulé datum) - stejný lesson jako
    # scripts/build_candidate_dataset.py dřív mělo hardcoded datum (viz PLAN.md).
    target_date = datetime.now(timezone.utc).date() + timedelta(days=1)
    date_str = target_date.isoformat()
    hours = [f"{date_str}T{h:02d}:00" for h in range(24)]

    def fake_get(url, params=None, timeout=None):
        return _FakeResponse(200, _fake_open_meteo_payload(hours))

    monkeypatch.setattr(requests, "get", fake_get)
    target = datetime(target_date.year, target_date.month, target_date.day, 18, 43, tzinfo=timezone.utc)
    result = fetch_weather_forecast(49.5, 18.4, target, timezone_name="UTC")
    assert result.forecast_time.hour == 19  # nejbližší celá hodina k 18:43 UTC
    assert result.cloud_cover_pct == 20.0
    assert result.weather_condition == "mainly clear"


def test_fetch_weather_forecast_raises_on_network_error(monkeypatch):
    def fake_get(url, params=None, timeout=None):
        raise requests.exceptions.ConnectionError("down")

    monkeypatch.setattr(requests, "get", fake_get)
    with pytest.raises(WeatherUnavailableError):
        fetch_weather_forecast(49.5, 18.4, datetime.now(timezone.utc), timezone_name="UTC")


def test_fetch_weather_forecast_raises_on_far_future_date(monkeypatch):
    with pytest.raises(WeatherUnavailableError):
        fetch_weather_forecast(49.5, 18.4, datetime.now(timezone.utc) + timedelta(days=30), timezone_name="UTC")


def test_fetch_weather_forecast_raises_when_no_close_hour(monkeypatch):
    target_date = datetime.now(timezone.utc).date() + timedelta(days=1)
    hours = [f"{target_date.isoformat()}T00:00"]  # jen jedna hodina, daleko od cíle (18:00)

    def fake_get(url, params=None, timeout=None):
        return _FakeResponse(200, _fake_open_meteo_payload(hours))

    monkeypatch.setattr(requests, "get", fake_get)
    target = datetime(target_date.year, target_date.month, target_date.day, 18, 0, tzinfo=timezone.utc)
    with pytest.raises(WeatherUnavailableError):
        fetch_weather_forecast(49.5, 18.4, target, timezone_name="UTC")


def test_fetch_weather_forecast_batch_covers_multiple_days_with_one_request(monkeypatch):
    # "Best days this week" (zadání bod 3/4) - jeden request musí pokrýt
    # VŠECHNY požadované dny, ne jeden request na den.
    base_date = datetime.now(timezone.utc).date() + timedelta(days=1)
    hours = []
    for day_offset in range(6):
        d = base_date + timedelta(days=day_offset)
        hours.extend(f"{d.isoformat()}T{h:02d}:00" for h in range(24))

    request_count = {"n": 0}

    def fake_get(url, params=None, timeout=None):
        request_count["n"] += 1
        return _FakeResponse(200, _fake_open_meteo_payload(hours))

    monkeypatch.setattr(requests, "get", fake_get)
    targets = [
        datetime(base_date.year, base_date.month, base_date.day, 19, 0, tzinfo=timezone.utc) + timedelta(days=i)
        for i in range(5)
    ]
    result = fetch_weather_forecast_batch(49.5, 18.4, targets, timezone_name="UTC")
    assert request_count["n"] == 1  # jeden sdílený request pro všech 5 dnů
    assert len(result) == 5
    for t in targets:
        assert result[t].forecast_time.hour == 19


def test_fetch_weather_forecast_batch_raises_on_far_future_date():
    targets = [datetime.now(timezone.utc) + timedelta(days=1), datetime.now(timezone.utc) + timedelta(days=30)]
    with pytest.raises(WeatherUnavailableError):
        fetch_weather_forecast_batch(49.5, 18.4, targets, timezone_name="UTC")


def _fake_weather(cloud=20.0):
    return WeatherInfo(
        latitude=49.5, longitude=18.4, forecast_time=datetime(2026, 9, 25, 19, tzinfo=timezone.utc),
        cloud_cover_pct=cloud, cloud_cover_low_pct=5.0, cloud_cover_mid_pct=10.0, cloud_cover_high_pct=5.0,
        visibility_m=35000.0, precipitation_probability_pct=0.0, precipitation_mm=0.0, temperature_c=15.0,
        wind_speed_kmh=8.0, wind_direction_deg=270.0, humidity_pct=55.0, pressure_hpa=1013.0,
        weather_code=1, weather_condition="mainly clear", retrieved_at=datetime.now(timezone.utc),
    )


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "weather_test.sqlite3"
    init_db(path)
    return path


def test_weather_cache_roundtrip(db):
    target = datetime(2026, 9, 25, 18, 43, tzinfo=timezone.utc)
    save_weather(49.5, 18.4, target, _fake_weather(), db_path=db)
    cached = get_cached_weather(49.5, 18.4, target, db_path=db)
    assert cached is not None
    assert cached.cloud_cover_pct == 20.0


def test_weather_cache_miss_for_unknown_location(db):
    target = datetime(2026, 9, 25, 18, 43, tzinfo=timezone.utc)
    assert get_cached_weather(60.0, 30.0, target, db_path=db) is None


def test_weather_cache_respects_ttl(db):
    target = datetime(2026, 9, 25, 18, 43, tzinfo=timezone.utc)
    weather = _fake_weather()
    weather.retrieved_at = datetime.now(timezone.utc) - timedelta(hours=5)
    save_weather(49.5, 18.4, target, weather, db_path=db)
    assert get_cached_weather(49.5, 18.4, target, ttl=timedelta(hours=3), db_path=db) is None  # stale
    assert get_cached_weather(49.5, 18.4, target, ttl=timedelta(hours=6), db_path=db) is not None  # v rámci delšího TTL


def test_fetch_weather_cached_hits_cache_on_second_call(db, monkeypatch):
    call_count = {"n": 0}

    def fake_fetch(lat, lon, target_time, timezone_name="UTC"):
        call_count["n"] += 1
        return _fake_weather()

    monkeypatch.setattr("app.weather_cache.fetch_weather_forecast", fake_fetch)
    target = datetime(2026, 9, 25, 18, 43, tzinfo=timezone.utc)
    fetch_weather_cached(49.5, 18.4, target, db_path=db)
    fetch_weather_cached(49.5, 18.4, target, db_path=db)
    assert call_count["n"] == 1  # druhé volání muselo přijít z cache, ne z (mockovaného) API


def test_nearby_coordinates_share_cache_entry(db):
    # zaokrouhlení na 2 desetinná místa (~1.1 km) - zadání "vhodně cachuj"
    target = datetime(2026, 9, 25, 18, 43, tzinfo=timezone.utc)
    save_weather(49.5001, 18.4001, target, _fake_weather(), db_path=db)
    assert get_cached_weather(49.5002, 18.4002, target, db_path=db) is not None


def test_fetch_weather_batch_cached_only_fetches_misses(db, monkeypatch):
    targets = [datetime(2026, 9, 25, 18, 43, tzinfo=timezone.utc) + timedelta(days=i) for i in range(4)]
    # Den 1 už je v cachi (staršího requestu) - nemusí se znovu stahovat.
    save_weather(49.5, 18.4, targets[1], _fake_weather(), db_path=db)

    batch_calls = {"n": 0, "requested": None}

    def fake_batch(lat, lon, target_times, timezone_name="UTC"):
        batch_calls["n"] += 1
        batch_calls["requested"] = list(target_times)
        return {t: _fake_weather() for t in target_times}

    monkeypatch.setattr("app.weather_cache.fetch_weather_forecast_batch", fake_batch)
    result = fetch_weather_batch_cached(49.5, 18.4, targets, db_path=db)
    assert len(result) == 4
    assert batch_calls["n"] == 1  # jeden sdílený request pro VŠECHNY cache misses
    assert targets[1] not in batch_calls["requested"]  # ten den byl už v cachi, neposílá se znovu
    assert len(batch_calls["requested"]) == 3


def test_fetch_weather_batch_cached_skips_network_when_all_cached(db):
    targets = [datetime(2026, 9, 25, 18, 43, tzinfo=timezone.utc) + timedelta(days=i) for i in range(3)]
    for t in targets:
        save_weather(49.5, 18.4, t, _fake_weather(), db_path=db)
    result = fetch_weather_batch_cached(49.5, 18.4, targets, db_path=db)  # žádný fetch_weather_forecast_batch mock -> spadlo by na síť, kdyby se volalo
    assert len(result) == 3
