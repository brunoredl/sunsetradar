"""Testy pro app/geocode_cache.py (oprava produkčního nálezu na Render
Free - Nominatim 429 dřív splácal na matoucí 502, žádná cache). `requests.get`
je mockovaný (žádná reálná síť v unit testech), stejný vzor jako
tests/test_weather.py."""

from __future__ import annotations

from datetime import timedelta

import pytest
import requests

from app.geocode_cache import (
    GeocodeNotFoundError,
    GeocodeRateLimitedError,
    GeocodeUnavailableError,
    _get_cached,
    geocode_cached,
    init_db,
)


class _FakeResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(f"{self.status_code}")

    def json(self):
        return self._payload


def _found_payload(lat="49.19", lon="16.61", name="Brno, Česko"):
    return [{"lat": lat, "lon": lon, "display_name": name}]


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "geocode_test.sqlite3"
    init_db(path)
    return path


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    # Throttle testuje samostatný test níž - jinde by jen zbytečně
    # zpomaloval zbytek test suite (zadání "max 1 req/s", ne "test suite
    # musí čekat sekundy navíc").
    import app.geocode_cache as geocode_cache_module

    monkeypatch.setattr(geocode_cache_module, "MIN_REQUEST_INTERVAL_S", 0.0)
    monkeypatch.setattr(geocode_cache_module, "_last_request_monotonic", 0.0)


def test_geocode_cached_returns_coordinates_on_success(monkeypatch, db):
    def fake_get(url, params=None, headers=None, timeout=None):
        assert params["countrycodes"] == "cz"
        assert "SunsetRadar" in headers["User-Agent"]  # identifikující UA, zadání bod 2
        return _FakeResponse(200, _found_payload())

    monkeypatch.setattr("app.geocode_cache.requests.get", fake_get)
    result = geocode_cached("Brno", db_path=db)
    assert result["lat"] == pytest.approx(49.19)
    assert result["lon"] == pytest.approx(16.61)
    assert result["display_name"] == "Brno, Česko"


def test_geocode_cached_raises_not_found_for_empty_results(monkeypatch, db):
    monkeypatch.setattr("app.geocode_cache.requests.get", lambda *a, **k: _FakeResponse(200, []))
    with pytest.raises(GeocodeNotFoundError):
        geocode_cached("xxNoSuchPlacexx", db_path=db)


def test_geocode_cached_raises_rate_limited_on_429(monkeypatch, db):
    monkeypatch.setattr("app.geocode_cache.requests.get", lambda *a, **k: _FakeResponse(429, None))
    with pytest.raises(GeocodeRateLimitedError):
        geocode_cached("Brno", db_path=db)


def test_geocode_cached_raises_unavailable_on_connection_error(monkeypatch, db):
    def fake_get(*a, **k):
        raise requests.exceptions.ConnectionError("network down")

    monkeypatch.setattr("app.geocode_cache.requests.get", fake_get)
    with pytest.raises(GeocodeUnavailableError):
        geocode_cached("Brno", db_path=db)


def test_geocode_cached_raises_unavailable_on_other_http_error(monkeypatch, db):
    monkeypatch.setattr("app.geocode_cache.requests.get", lambda *a, **k: _FakeResponse(500, None))
    with pytest.raises(GeocodeUnavailableError):
        geocode_cached("Brno", db_path=db)


# --- Zadání bod 3 - "opakované hledání stejného místa nesmí zbytečně
# generovat další externí požadavky" ---


def test_repeated_identical_query_does_not_call_nominatim_again(monkeypatch, db):
    call_count = {"n": 0}

    def fake_get(*a, **k):
        call_count["n"] += 1
        return _FakeResponse(200, _found_payload())

    monkeypatch.setattr("app.geocode_cache.requests.get", fake_get)
    first = geocode_cached("Brno", db_path=db)
    second = geocode_cached("Brno", db_path=db)
    assert call_count["n"] == 1
    assert first == second


def test_query_normalization_shares_one_cache_entry(monkeypatch, db):
    # Mezery/velikost písmen nesmí vytvořit zbytečně samostatné cache
    # záznamy pro fakticky stejný dotaz.
    call_count = {"n": 0}

    def fake_get(*a, **k):
        call_count["n"] += 1
        return _FakeResponse(200, _found_payload())

    monkeypatch.setattr("app.geocode_cache.requests.get", fake_get)
    geocode_cached("Brno", db_path=db)
    geocode_cached("  brno  ", db_path=db)
    geocode_cached("BRNO", db_path=db)
    assert call_count["n"] == 1


def test_not_found_result_is_also_cached_and_does_not_repeat_the_request(monkeypatch, db):
    # "Nenalezeno" se taky cachuje (zadání bod 3 platí i pro negativní
    # výsledky - opakované hledání překlepu by jinak pořád bušilo do
    # Nominatim), jen s kratším TTL než nalezené místo (viz NOT_FOUND_TTL).
    call_count = {"n": 0}

    def fake_get(*a, **k):
        call_count["n"] += 1
        return _FakeResponse(200, [])

    monkeypatch.setattr("app.geocode_cache.requests.get", fake_get)
    for _ in range(3):
        with pytest.raises(GeocodeNotFoundError):
            geocode_cached("xxNoSuchPlacexx", db_path=db)
    assert call_count["n"] == 1


def test_rate_limited_response_is_not_cached_so_next_attempt_can_retry(monkeypatch, db):
    # 429 je PŘECHODNÁ chyba - nesmí se natrvalo "zaseknout" v cache
    # jako by místo neexistovalo (jinak by appka ztratila funkčnost pro
    # tenhle dotaz, dokud TTL nevyprší).
    call_count = {"n": 0}

    def fake_get(*a, **k):
        call_count["n"] += 1
        return _FakeResponse(429, None)

    monkeypatch.setattr("app.geocode_cache.requests.get", fake_get)
    with pytest.raises(GeocodeRateLimitedError):
        geocode_cached("Brno", db_path=db)
    with pytest.raises(GeocodeRateLimitedError):
        geocode_cached("Brno", db_path=db)
    assert call_count["n"] == 2  # obě selhání šla skutečně ven, žádné se tiše "nezacyklilo"


def test_stale_cache_entry_past_ttl_triggers_fresh_request(monkeypatch, db):
    from datetime import datetime, timezone

    from app.geocode_cache import _save_cached

    # Ulož záznam jako kdyby byl starý přes TTL (cached_at v minulosti).
    stale_time = (datetime.now(timezone.utc) - timedelta(days=31)).isoformat()
    import sqlite3

    conn = sqlite3.connect(str(db))
    conn.execute(
        "INSERT INTO geocode_cache (query_key, status, lat, lon, display_name, cached_at) VALUES (?, ?, ?, ?, ?, ?)",
        ("brno", "found", 49.0, 16.0, "stará hodnota", stale_time),
    )
    conn.commit()
    conn.close()

    call_count = {"n": 0}

    def fake_get(*a, **k):
        call_count["n"] += 1
        return _FakeResponse(200, _found_payload(name="čerstvá hodnota"))

    monkeypatch.setattr("app.geocode_cache.requests.get", fake_get)
    result = geocode_cached("Brno", db_path=db)
    assert call_count["n"] == 1
    assert result["display_name"] == "čerstvá hodnota"


# --- Zadání bod 2 - "rozumné omezení frekvence" vůči Nominatimu samotnému ---


def test_throttle_enforces_minimum_interval_between_real_requests(monkeypatch, db):
    import time

    import app.geocode_cache as geocode_cache_module

    monkeypatch.setattr(geocode_cache_module, "MIN_REQUEST_INTERVAL_S", 0.2)
    monkeypatch.setattr(geocode_cache_module, "_last_request_monotonic", 0.0)

    sleep_calls = []
    monkeypatch.setattr(geocode_cache_module.time, "sleep", lambda s: sleep_calls.append(s))

    call_times = []

    def fake_get(*a, **k):
        call_times.append(time.monotonic())
        return _FakeResponse(200, _found_payload())

    monkeypatch.setattr("app.geocode_cache.requests.get", fake_get)
    geocode_cached("Brno", db_path=db)
    geocode_cached("Praha", db_path=db)  # jiný dotaz = jiný cache klíč, skutečně druhý request
    assert len(sleep_calls) == 1  # druhý požadavek musel počkat
    assert sleep_calls[0] > 0
