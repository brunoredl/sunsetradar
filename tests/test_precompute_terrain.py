"""Testy pro scripts/precompute_terrain.py (Milestone "Smart National
Terrain Precompute"). Čisté funkce (`select_candidates`/
`prioritize_candidates`/`_in_bbox`) se testují přímo nad malými
deterministickými seznamy dictů - žádná reálná SQLite/DEM/síť. Pár
testů běží přes `main()` (monkeypatched `sys.argv` + `capsys`), aby
ověřily CLI glue (--limit/--dry-run/--region/--near-lat/--candidate-type)
end-to-end, pořád bez reálného I/O (terrain_cache/catalog_db/
compute_static_terrain jsou mockované)."""

from __future__ import annotations

import pytest

from scripts import precompute_terrain as pt


def _candidate(id_, region, candidate_type="peak", near_city=None, elevation_m=None, lat=50.0, lon=14.0):
    return {
        "id": id_, "slug": id_, "name": id_, "display_name": id_, "candidate_type": candidate_type,
        "region": region, "near_city": near_city, "elevation_m": elevation_m, "lat": lat, "lon": lon,
    }


# --- select_candidates() - stavový filtr (ready/running/failed) ---


def test_select_skips_ready_without_force(monkeypatch):
    catalog = [_candidate("a", "grid_00_00"), _candidate("b", "grid_00_00")]
    monkeypatch.setattr(pt.terrain_cache, "get_status", lambda cid, key: "ready" if cid == "a" else "missing")
    to_process, skipped_ready, skipped_running = pt.select_candidates(catalog, "key", force=False)
    assert [c["id"] for c in to_process] == ["b"]
    assert skipped_ready == 1 and skipped_running == 0


def test_select_force_includes_ready(monkeypatch):
    catalog = [_candidate("a", "grid_00_00")]
    monkeypatch.setattr(pt.terrain_cache, "get_status", lambda cid, key: "ready")
    to_process, skipped_ready, _ = pt.select_candidates(catalog, "key", force=True)
    assert [c["id"] for c in to_process] == ["a"]
    assert skipped_ready == 0


def test_select_always_skips_running_even_with_force(monkeypatch):
    # zadání bod 2/4 - "nesmí způsobit kolize s běžícím workerem" -
    # 'running' se přeskakuje VŽDY, --force na něj nemá vliv (na rozdíl
    # od 'ready').
    catalog = [_candidate("a", "grid_00_00")]
    monkeypatch.setattr(pt.terrain_cache, "get_status", lambda cid, key: "running")
    to_process, skipped_ready, skipped_running = pt.select_candidates(catalog, "key", force=True)
    assert to_process == []
    assert skipped_running == 1


def test_select_retries_failed_not_permanently_blacklisted(monkeypatch):
    catalog = [_candidate("a", "grid_00_00")]
    monkeypatch.setattr(pt.terrain_cache, "get_status", lambda cid, key: "failed")
    to_process, _, _ = pt.select_candidates(catalog, "key", force=False)
    assert [c["id"] for c in to_process] == ["a"]  # failed dostane novou šanci, beze --force


def test_select_candidate_type_filter(monkeypatch):
    catalog = [_candidate("a", "grid_00_00", candidate_type="peak"), _candidate("b", "grid_00_00", candidate_type="viewpoint")]
    monkeypatch.setattr(pt.terrain_cache, "get_status", lambda cid, key: "missing")
    to_process, _, _ = pt.select_candidates(catalog, "key", candidate_type="viewpoint")
    assert [c["id"] for c in to_process] == ["b"]


# --- prioritize_candidates() - deterministická "smart" priorita ---


def test_prioritize_round_robins_across_regions_before_exhausting_one():
    # zadání bod 3 - "postupně zpracovávat různá významná území, ne jen
    # okolí jednoho města": region grid_00_00 má 2 kandidáty (a1, a2),
    # grid_00_01 má 1 (b1) - DRUHÝ kandidát z grid_00_00 (ať je
    # kterýkoliv, podle vnitřního řazení skupiny) nesmí přijít před
    # jediným kandidátem z grid_00_01 (round-robin, ne "vyprázdni
    # region A, pak přejdi na B").
    catalog = [
        _candidate("a1", "grid_00_00", elevation_m=100),
        _candidate("a2", "grid_00_00", elevation_m=900),  # vyšší elevace -> uvnitř grid_00_00 první (viz within-group test)
        _candidate("b1", "grid_00_01", elevation_m=50),
    ]
    ordered = pt.prioritize_candidates(catalog)
    assert [c["id"] for c in ordered] == ["a2", "b1", "a1"]  # a2 (region A, kolo 1), b1 (region B, kolo 1), a1 (region A, kolo 2)


def test_prioritize_prefers_peak_viewpoint_tower_over_other_types():
    catalog = [
        _candidate("castle", "grid_00_00", candidate_type="castle", elevation_m=1000),
        _candidate("peak", "grid_00_00", candidate_type="peak", elevation_m=10),
    ]
    ordered = pt.prioritize_candidates(catalog)
    assert [c["id"] for c in ordered] == ["peak", "castle"]  # typ vyhrává i proti nižší elevaci


def test_prioritize_prefers_near_city_within_same_type_tier():
    catalog = [
        _candidate("far", "grid_00_00", near_city=False, elevation_m=1000),
        _candidate("near", "grid_00_00", near_city=True, elevation_m=10),
    ]
    ordered = pt.prioritize_candidates(catalog)
    assert [c["id"] for c in ordered] == ["near", "far"]


def test_prioritize_falls_back_to_elevation_then_id_for_determinism():
    catalog = [_candidate("z", "grid_00_00", elevation_m=500), _candidate("a", "grid_00_00", elevation_m=500)]
    ordered = pt.prioritize_candidates(catalog)
    assert [c["id"] for c in ordered] == ["a", "z"]  # shodná elevace -> id jako poslední tie-break


def test_prioritize_is_deterministic_across_repeated_calls():
    catalog = [_candidate(f"c{i}", f"grid_00_0{i % 4}", elevation_m=float(i)) for i in range(12)]
    orders = {tuple(c["id"] for c in pt.prioritize_candidates(catalog)) for _ in range(5)}
    assert len(orders) == 1


def test_prioritize_missing_region_goes_into_own_group_not_lost():
    catalog = [_candidate("no_region", None), _candidate("has_region", "grid_00_00")]
    ordered = pt.prioritize_candidates(catalog)
    assert {c["id"] for c in ordered} == {"no_region", "has_region"}  # nikdy se neztratí


# --- _in_bbox() - geografický filtr regionálního režimu ---


def test_in_bbox_true_for_point_inside():
    assert pt._in_bbox({"lat": 49.5, "lon": 18.5}, (49.4, 18.3, 49.7, 18.7))


def test_in_bbox_false_for_point_outside():
    assert not pt._in_bbox({"lat": 51.0, "lon": 18.5}, (49.4, 18.3, 49.7, 18.7))


# --- main() - CLI glue, s mockovaným catalog/terrain_cache/compute (capsys) ---


@pytest.fixture
def fake_main_env(monkeypatch):
    """Malý národní katalog (3 grid regiony) + terrain_cache/compute
    mock - stejný duch jako `fake_catalog`/`fake_cache` v test_web.py."""
    catalog = [
        _candidate("praha_peak", "grid_01_02", candidate_type="peak", near_city=True, elevation_m=400, lat=50.08, lon=14.44),
        _candidate("beskydy_peak", "grid_01_06", candidate_type="peak", near_city=False, elevation_m=1300, lat=49.5, lon=18.45),
        _candidate("beskydy_view", "grid_01_06", candidate_type="viewpoint", near_city=True, elevation_m=None, lat=49.55, lon=18.5),
    ]
    status: dict[str, str] = {}
    compute_calls: list[str] = []

    monkeypatch.setattr(pt, "load_candidate_catalog", lambda use_cache=False: catalog)
    monkeypatch.setattr(pt.terrain_cache, "init_db", lambda: None)
    monkeypatch.setattr(pt.terrain_cache, "compute_cache_key", lambda: "fake-key")
    monkeypatch.setattr(pt.terrain_cache, "get_status", lambda cid, key: status.get(cid, "missing"))
    monkeypatch.setattr(pt.terrain_cache, "count_by_status", lambda cache_key=None: {})

    def fake_save_ready(cid, key, lat, lon, static, dt, region=None, candidate_type=None):
        status[cid] = "ready"
        return True

    def fake_save_failed(cid, key, lat, lon, msg):
        status[cid] = "failed"

    monkeypatch.setattr(pt.terrain_cache, "save_ready", fake_save_ready)
    monkeypatch.setattr(pt.terrain_cache, "save_failed", fake_save_failed)

    def fake_compute(lat, lon):
        compute_calls.append((lat, lon))
        from app.analyze_location import DemInfo, LocationInfo, StaticTerrain, TerrainInfo
        from app.horizon import HorizonProfile
        import numpy as np
        from datetime import datetime, timezone

        az = np.arange(0.0, 360.0, 1.0)
        alt = np.full_like(az, -1.0)
        return StaticTerrain(
            location=LocationInfo(latitude=lat, longitude=lon, elevation_m=500.0),
            dem=DemInfo(provider="test", dataset="test", resolution_m=10.0, analysis_range_m=50000.0, source_crs="EPSG:4326",
                        acquired_at=datetime(2026, 10, 18, tzinfo=timezone.utc), fallback_used=False, fallback_reason=None, quality_flag="ok"),
            terrain=TerrainInfo(azimuths_deg=[float(a) for a in az], horizon_altitudes_deg=[float(a) for a in alt],
                                 minimum_horizon_deg=-1.0, maximum_horizon_deg=-1.0, slope_deg=0.0, aspect_deg=0.0,
                                 _profile=HorizonProfile(azimuths_deg=az, horizon_altitude_deg=alt)),
            warnings=[],
        )

    monkeypatch.setattr(pt, "compute_static_terrain", fake_compute)

    def fake_radius_search(lat, lon, radius_km, candidate_type=None, db_path=None):
        out = []
        for c in catalog:
            if candidate_type and c["candidate_type"] != candidate_type:
                continue
            # jednoduchý čtvercový "radius" dost velký na testovací souřadnice - přesná geometrie testuje app/catalog_db.py samo
            if abs(c["lat"] - lat) < 1.0 and abs(c["lon"] - lon) < 1.0:
                out.append({**c, "distance_km": 1.0})
        return out

    monkeypatch.setattr(pt.catalog_db, "radius_search", fake_radius_search)

    return {"catalog": catalog, "status": status, "compute_calls": compute_calls}


def _run(monkeypatch, args: list[str]):
    monkeypatch.setattr("sys.argv", ["precompute_terrain.py", *args])
    pt.main()


def test_main_national_mode_selects_from_whole_catalog(monkeypatch, fake_main_env, capsys):
    _run(monkeypatch, ["--limit", "10", "--dry-run"])
    out = capsys.readouterr().out
    assert "celostátní katalog" in out
    assert "Kandidátů v oblasti po typovém filtru: 3" in out
    assert fake_main_env["compute_calls"] == []  # dry-run nikdy nepočítá


def test_main_dry_run_never_writes_cache(monkeypatch, fake_main_env, capsys):
    _run(monkeypatch, ["--dry-run"])
    assert fake_main_env["status"] == {}  # žádný save_ready/save_failed se nezavolal
    assert fake_main_env["compute_calls"] == []


def test_main_limit_caps_batch_size(monkeypatch, fake_main_env, capsys):
    _run(monkeypatch, ["--limit", "1", "--dry-run"])
    out = capsys.readouterr().out
    assert "K zpracování v tomhle běhu (po prioritizaci + limitu): 1." in out


def test_main_region_bbox_filter_matches_old_region_argument(monkeypatch, fake_main_env, capsys):
    # Regresní test přesně na bug popsaný v docstringu skriptu: --region
    # beskydy musí najít kandidáta v grid_01_06, který leží ve
    # skutečném Beskydském bboxu, i když jeho `region` pole říká
    # "grid_01_06", ne "beskydy".
    _run(monkeypatch, ["--region", "beskydy", "--dry-run"])
    out = capsys.readouterr().out
    assert "Kandidátů v oblasti po typovém filtru: 2" in out  # beskydy_peak + beskydy_view, ne praha_peak


def test_main_near_lat_lon_uses_radius_search(monkeypatch, fake_main_env, capsys):
    _run(monkeypatch, ["--near-lat", "50.08", "--near-lon", "14.44", "--near-radius-km", "10", "--dry-run"])
    out = capsys.readouterr().out
    assert "praha_peak" in out
    assert "beskydy_peak" not in out


def test_main_candidate_type_filter(monkeypatch, fake_main_env, capsys):
    _run(monkeypatch, ["--candidate-type", "viewpoint", "--dry-run"])
    out = capsys.readouterr().out
    assert "beskydy_view" in out
    assert "praha_peak" not in out and "beskydy_peak" not in out


def test_main_region_and_near_lat_are_mutually_exclusive(monkeypatch, capsys):
    _run(monkeypatch, ["--region", "beskydy", "--near-lat", "50.0", "--near-lon", "14.0"])
    out = capsys.readouterr().out
    assert "vzájemně vylučují" in out


def test_main_repeated_run_skips_already_ready_without_force(monkeypatch, fake_main_env, capsys):
    _run(monkeypatch, ["--candidate-type", "peak", "--region", "beskydy"])  # reálný běh (bez --dry-run) na 1 kandidáta
    assert fake_main_env["status"]["beskydy_peak"] == "ready"
    assert len(fake_main_env["compute_calls"]) == 1

    _run(monkeypatch, ["--candidate-type", "peak", "--region", "beskydy"])  # druhé spuštění
    out = capsys.readouterr().out
    assert "Přeskočeno (už 'ready', bez --force): 1." in out
    assert len(fake_main_env["compute_calls"]) == 1  # beze změny - nic se nepočítalo znovu


def test_main_force_recomputes_ready_candidate(monkeypatch, fake_main_env):
    _run(monkeypatch, ["--candidate-type", "peak", "--region", "beskydy"])
    assert len(fake_main_env["compute_calls"]) == 1
    _run(monkeypatch, ["--candidate-type", "peak", "--region", "beskydy", "--force"])
    assert len(fake_main_env["compute_calls"]) == 2  # --force přepočítá i 'ready'
