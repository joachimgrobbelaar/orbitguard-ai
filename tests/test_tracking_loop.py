"""Tests for the OrbitGuard real-time tracking loop engine and ObservationSource plug-ins."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import math
from pathlib import Path
import sqlite3
import threading
import time
from typing import Any, Optional

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from orbitguard.main import app as main_app
from orbitguard.monitor.api import build_test_router
from orbitguard.monitor.ingest import CatalogStore, HttpResult
from orbitguard.monitor.loop import (
    COLOCATED_REASON,
    LEO_MAX_PERIGEE_ALTITUDE_KM,
    SCREENING_LABEL,
    TrackingLoop,
    altitude_bands_overlap,
    is_leo_orbit,
    orbital_period_minutes,
    orbital_period_seconds,
    perigee_apogee_altitude_km,
)
from orbitguard.monitor.sensors import (
    DEFAULT_LOOP_GROUPS,
    SNAPSHOT_ONLY_STATIONS_NOTE,
    CatalogElementSetSource,
    LidarObservationSource,
    NotConnected,
    ObservationBatch,
    ObservationSource,
    SourceStatus,
    VisionObservationSource,
    configured_loop_groups,
    sensor_catalog,
)
from orbitguard.monitor.service import MonitorService
from orbitguard.nd import parse_epoch

STATIONS_META = json.loads(Path("data/gp/stations.meta.json").read_text(encoding="utf-8"))
STATIONS_RETRIEVED_AT = str(STATIONS_META["retrieved_at"])
STATIONS_RETRIEVED_DT = parse_epoch(STATIONS_RETRIEVED_AT)


def _client_with_loop(tmp_path: Path, **loop_kwargs: Any) -> tuple[TestClient, TrackingLoop]:
    service = MonitorService(
        tmp_path,
        transport=lambda url: HttpResult(504, b"network disabled in tests", url),
    )
    loop = TrackingLoop(tmp_path, store=service.store, **loop_kwargs)
    service.loop = loop
    app = FastAPI()
    app.include_router(build_test_router(service, loop=loop))
    return TestClient(app), loop


def _assert_no_probability_value(payload: Any, path: str = "root") -> None:
    """Recursively verify that no collision probability key or numeric probability value is output."""
    forbidden_keys = {"pc", "probability", "collision_probability", "nc"}
    if isinstance(payload, dict):
        for key, value in payload.items():
            lowered = str(key).lower()
            assert lowered not in forbidden_keys, f"Forbidden probability key {key!r} at {path}.{key}"
            _assert_no_probability_value(value, f"{path}.{key}")
    elif isinstance(payload, list):
        for idx, item in enumerate(payload):
            _assert_no_probability_value(item, f"{path}[{idx}]")


def test_lidar_and_vision_sources_are_not_connected_and_yield_no_data():
    lidar = LidarObservationSource()
    vision = VisionObservationSource()

    assert isinstance(lidar, ObservationSource)
    assert isinstance(vision, ObservationSource)
    assert lidar.name == "lidar"
    assert vision.name == "xvision"

    lidar_status = lidar.status()
    vision_status = vision.status()
    assert lidar_status == "not_connected"
    assert lidar_status["status"] == "not_connected"
    assert lidar_status["connected"] is False
    assert lidar_status["observations_produced"] == 0

    assert vision_status == "not_connected"
    assert vision_status["status"] == "not_connected"
    assert vision_status["connected"] is False
    assert vision_status["observations_produced"] == 0

    with pytest.raises(NotConnected):
        lidar.fetch()
    with pytest.raises(NotConnected):
        vision.fetch()

    # Docstrings must document measurement formats and required orbit determination
    assert lidar.__doc__ is not None
    assert "orbit determination" in lidar.__doc__.lower()
    assert "range" in lidar.__doc__.lower()
    assert vision.__doc__ is not None
    assert "orbit determination" in vision.__doc__.lower()
    assert "angles-only" in vision.__doc__.lower()

    # Existing sensor_catalog() output remains intact
    cat = sensor_catalog()
    assert cat["feed_status"] == "disconnected"
    assert len(cat["sensors"]) == 6


def test_run_once_snapshot_fallback_and_sqlite_persistence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ORBITGUARD_LOOP_LIVE_FETCH", "0")
    now = datetime(2026, 11, 12, 16, 0, 0, tzinfo=timezone.utc)

    # Use threshold_km=500.0 so both co-located pairs (38 docked pairs) and real close
    # approaches (8 pairs between FREGAT DEB and the ISS assembly at ~361.9 km) are exercised.
    loop = TrackingLoop(tmp_path, step_s=30.0, threshold_km=500.0)
    report = loop.run_once(now=now)

    expected_snap_age = (now - STATIONS_RETRIEVED_DT).total_seconds()

    assert report["status"] == "ok"
    assert report["iteration_id"] == 1
    assert report["source_mode"] == "snapshot_fallback"
    assert report["source_name"] == "celestrak_stations_snapshot"
    assert report["retrieved_at"] == STATIONS_RETRIEVED_AT
    assert "ORBITGUARD_LOOP_LIVE_FETCH=0" in (report["fallback_reason"] or "")
    assert report["snapshot_age_s"] == pytest.approx(expected_snap_age)
    assert report["data_age_s"] == pytest.approx(expected_snap_age)
    assert report["object_count"] == 19
    assert report["propagated_count"] == 19
    assert report["reused_count"] == 0
    assert report["co_located_count"] == 38
    assert report["conjunction_count"] == 8
    assert report["screened_pair_count"] + report["skipped_pair_count"] == 19 * 18 // 2
    assert (
        report["radial_skipped_pair_count"] + report["spatial_skipped_pair_count"]
        == report["skipped_pair_count"]
    )
    assert report["label"] == SCREENING_LABEL
    assert report["error"] is None

    # Verify orbital period (1440 / n minutes) and 1/8 period horizon and cadence
    snapshot_records = json.loads(Path("data/gp/stations.json").read_text(encoding="utf-8"))
    expected_periods_s = [orbital_period_seconds(float(r["MEAN_MOTION"])) for r in snapshot_records]
    expected_min_period_s = min(expected_periods_s)
    expected_min_horizon_s = expected_min_period_s / 8.0
    assert report["shortest_period_s"] == pytest.approx(expected_min_period_s)
    assert report["min_horizon_s"] == pytest.approx(expected_min_horizon_s)
    assert report["cadence_s"] == pytest.approx(expected_min_horizon_s)
    assert orbital_period_minutes(15.0) == pytest.approx(96.0)

    # Verify SQLite file and required tables
    db_path = tmp_path / "tracking_loop.sqlite"
    assert db_path.is_file()
    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        iter_row = conn.execute("SELECT * FROM iterations WHERE id = 1").fetchone()
        assert iter_row is not None
        assert iter_row["source_mode"] == "snapshot_fallback"
        assert iter_row["object_count"] == 19
        assert iter_row["conjunction_count"] == 8
        assert iter_row["co_located_count"] == 38
        assert iter_row["error"] is None

        elem_rows = conn.execute("SELECT * FROM element_sets WHERE iteration_id = 1").fetchall()
        assert len(elem_rows) == 19
        iss_row = next(r for r in elem_rows if r["norad_id"] == 25544)
        assert iss_row["name"] == "ISS (ZARYA)"
        assert len(iss_row["canonical_hash"]) == 64
        parsed_raw = json.loads(iss_row["raw_omm_json"])
        assert parsed_raw["NORAD_CAT_ID"] == 25544

        state_rows = conn.execute(
            "SELECT * FROM propagated_states WHERE iteration_id = 1 AND norad_id = 25544 ORDER BY t ASC"
        ).fetchall()
        assert len(state_rows) == int(iss_row["horizon_s"] // 30.0) + 1
        assert state_rows[0]["frame"] == "TEME"
        assert math.isfinite(state_rows[0]["x"])

        conj_rows = conn.execute("SELECT * FROM conjunction_screenings WHERE iteration_id = 1").fetchall()
        assert len(conj_rows) == 8
        assert all(0.0 < r["min_miss_km"] <= 500.0 for r in conj_rows)
        assert all(r["label"] == SCREENING_LABEL for r in conj_rows)

        coloc_rows = conn.execute("SELECT * FROM co_located_pairs WHERE iteration_id = 1").fetchall()
        assert len(coloc_rows) == 38
        assert all(r["reason"] == COLOCATED_REASON for r in coloc_rows)


def test_colocated_docked_vehicles_separated_from_real_conjunctions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Verify docked ISS and CSS vehicles in the real snapshot are classified as co_located."""
    monkeypatch.setenv("ORBITGUARD_LOOP_LIVE_FETCH", "0")
    now = datetime(2026, 11, 12, 16, 0, 0, tzinfo=timezone.utc)

    # At default 10 km threshold, all <10 km pairs in stations.json are docked assemblies
    loop = TrackingLoop(tmp_path, step_s=30.0, threshold_km=10.0)
    report = loop.run_once(now=now)
    assert report["conjunction_count"] == 0
    assert report["co_located_count"] == 38

    status = loop.status(now=now)
    assert status["conjunction_count"] == 0
    assert status["co_located_count"] == 38
    assert status["colocated_km"] == pytest.approx(5.0)
    assert status["colocated_rel_speed_km_s"] == pytest.approx(0.01)

    conj = loop.conjunctions(now=now)
    assert conj["count"] == 0
    assert conj["conjunction_count"] == 0
    assert conj["conjunctions"] == []
    assert conj["co_located_count"] == 38
    assert conj["co_located_reason"] == COLOCATED_REASON
    assert len(conj["co_located"]) == 38

    pairs_by_names = {
        (c["primary_name"], c["secondary_name"]): c for c in conj["co_located"]
    }
    assert ("ISS (ZARYA)", "PROGRESS-MS 34") in pairs_by_names
    assert ("CSS (TIANHE)", "CSS (MENGTIAN)") in pairs_by_names

    iss_prog = pairs_by_names[("ISS (ZARYA)", "PROGRESS-MS 34")]
    assert iss_prog["classification"] == "co_located"
    assert iss_prog["reason"] == COLOCATED_REASON
    assert iss_prog["min_miss_km"] <= 5.0
    assert iss_prog["max_miss_km"] <= 5.0
    assert iss_prog["max_relative_speed_km_s"] <= 0.01
    iss_poisk = pairs_by_names[("ISS (ZARYA)", "POISK")]
    assert iss_poisk["min_miss_km"] == pytest.approx(0.0, abs=1e-6)

    # Also test env var configuration of ORBITGUARD_LOOP_COLOCATED_KM and ORBITGUARD_LOOP_COLOCATED_REL_SPEED_KM_S
    monkeypatch.setenv("ORBITGUARD_LOOP_COLOCATED_KM", "2.5")
    monkeypatch.setenv("ORBITGUARD_LOOP_COLOCATED_REL_SPEED_KM_S", "0.005")
    loop_env = TrackingLoop(tmp_path / "env_coloc")
    assert loop_env.colocated_km == pytest.approx(2.5)
    assert loop_env.colocated_rel_speed_km_s == pytest.approx(0.005)


def test_live_vs_snapshot_ages_and_per_object_epoch_age_hours(tmp_path: Path):
    """Verify live data sets snapshot_age_s=None and populates data_age_s, plus per-object epoch_age_hours."""
    base_records = json.loads(Path("data/gp/stations.json").read_text(encoding="utf-8"))

    def live_transport(url: str) -> HttpResult:
        return HttpResult(status=200, body=json.dumps(base_records).encode("utf-8"), url=url)

    t_fetch = datetime(2026, 11, 12, 16, 0, 0, tzinfo=timezone.utc)
    loop_live = TrackingLoop(tmp_path / "live", transport=live_transport, live_fetch=True, step_s=30.0)
    run_res = loop_live.run_once(now=t_fetch)

    assert run_res["source_mode"] == "live"
    assert run_res["snapshot_age_s"] is None
    assert run_res["data_age_s"] == pytest.approx(0.0)
    assert run_res["provenance"]["snapshot_age_s"] is None
    assert run_res["provenance"]["data_age_s"] == pytest.approx(0.0)

    # Query status, objects, and conjunctions 45 seconds after retrieval
    t_query = t_fetch + timedelta(seconds=45)
    st = loop_live.status(now=t_query)
    assert st["snapshot_age_s"] is None
    assert st["data_age_s"] == pytest.approx(45.0)
    assert st["provenance"]["snapshot_age_s"] is None
    assert st["provenance"]["data_age_s"] == pytest.approx(45.0)

    objs = loop_live.objects(now=t_query)
    assert objs["snapshot_age_s"] is None
    assert objs["data_age_s"] == pytest.approx(45.0)
    assert objs["provenance"]["snapshot_age_s"] is None
    assert objs["provenance"]["data_age_s"] == pytest.approx(45.0)

    iss = next(o for o in objs["objects"] if o["norad_id"] == 25544)
    assert iss["snapshot_age_s"] is None
    assert iss["data_age_s"] == pytest.approx(45.0)
    expected_epoch_dt = parse_epoch(iss["epoch"])
    expected_epoch_age_h = (t_query - expected_epoch_dt).total_seconds() / 3600.0
    assert iss["epoch_age_hours"] == pytest.approx(expected_epoch_age_h)
    assert iss["epoch_age_h"] == pytest.approx(expected_epoch_age_h)
    assert iss["epoch_age_hours"] > 5.0

    conjs = loop_live.conjunctions(now=t_query)
    assert conjs["snapshot_age_s"] is None
    assert conjs["data_age_s"] == pytest.approx(45.0)
    assert conjs["provenance"]["snapshot_age_s"] is None
    assert conjs["provenance"]["data_age_s"] == pytest.approx(45.0)
    assert conjs["co_located"][0]["snapshot_age_s"] is None
    assert conjs["co_located"][0]["data_age_s"] == pytest.approx(45.0)


def test_multi_group_dedup_leo_filter_prefilter_and_fallback_provenance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Verify ORBITGUARD_LOOP_GROUPS, NORAD deduplication, LEO perigee filter, overlap prefilter, and non-stations fallback provenance."""
    monkeypatch.setenv("ORBITGUARD_LOOP_GROUPS", "stations, visual, active")
    assert configured_loop_groups() == ["stations", "visual", "active"]

    base_records = json.loads(Path("data/gp/stations.json").read_text(encoding="utf-8"))
    stations_subset = base_records[:4]  # ISS (25544), POISK (36086), CSS (48274), NAUKA (49044)

    # Create a second group ("visual") containing:
    # 1. Duplicate NORAD 25544 (ISS) with a NEWER epoch -> should win deduplication
    # 2. A non-LEO object (GEO / MEO with MEAN_MOTION = 2.0 rev/day -> perigee ~ 20,180 km >= 2000 km) -> should be filtered out
    # 3. A valid LEO object at ~800 km (DUPLEX from base_records[8])
    iss_newer = dict(base_records[0])
    iss_newer["EPOCH"] = "2026-11-12T12:00:00.000000"
    meo_object = dict(base_records[8])
    meo_object["OBJECT_NAME"] = "MEO TEST NAV"
    meo_object["NORAD_CAT_ID"] = 99991
    meo_object["MEAN_MOTION"] = 2.005  # Semi-major axis ~26,560 km, perigee > 20,000 km
    meo_object["ECCENTRICITY"] = 0.001
    leo_extra = dict(base_records[8])  # DUPLEX (66906)

    assert is_leo_orbit(float(iss_newer["MEAN_MOTION"]), float(iss_newer["ECCENTRICITY"])) is True
    assert is_leo_orbit(float(meo_object["MEAN_MOTION"]), float(meo_object["ECCENTRICITY"])) is False
    hp_meo, _ = perigee_apogee_altitude_km(float(meo_object["MEAN_MOTION"]), float(meo_object["ECCENTRICITY"]))
    assert hp_meo > LEO_MAX_PERIGEE_ALTITUDE_KM

    def multi_group_transport(url: str) -> HttpResult:
        if "GROUP=stations" in url:
            return HttpResult(200, json.dumps(stations_subset).encode("utf-8"), url)
        if "GROUP=visual" in url:
            return HttpResult(200, json.dumps([iss_newer, meo_object, leo_extra]).encode("utf-8"), url)
        # Simulate egress failure for group "active"
        return HttpResult(504, b"active group timed out", url)

    now = datetime(2026, 11, 12, 16, 0, 0, tzinfo=timezone.utc)
    loop = TrackingLoop(tmp_path / "multigroup", transport=multi_group_transport, live_fetch=True, step_s=30.0)
    report = loop.run_once(now=now)

    assert report["status"] == "ok"
    assert report["groups"] == ["stations", "visual", "active"]
    # 4 from stations + 3 from visual = 7 raw; 1 duplicate NORAD (25544) -> 6 unique; 1 MEO filtered -> 5 LEO objects
    assert report["deduplicated_count"] == 1
    assert report["non_leo_filtered_count"] == 1
    assert report["object_count"] == 5

    # Total pairs among 5 objects = 10.
    # ISS (25544), POISK (36086), NAUKA (49044) are at ~414-424 km (3 pairs overlap radially).
    # CSS (48274) is at ~380-386 km (doesn't overlap with 414 km or 335 km at 10 km pad).
    # DUPLEX (66906) is at ~335-342 km (doesn't overlap with 380 km or 414 km at 10 km pad).
    # Layer 1 (radial sieve) skips 7 pairs; Layer 2 (3D spatial hash) skips the 2 pairs
    # involving the epoch-shifted ISS (far in-track) and screens the 1 co-located POISK/NAUKA pair.
    assert report["total_pair_count"] == 10
    assert report["radial_skipped_pair_count"] == 7
    assert report["spatial_skipped_pair_count"] == 2
    assert report["skipped_pair_count"] == 9
    assert report["screened_pair_count"] == 1
    assert (
        report["screened_pair_count"]
        + report["radial_skipped_pair_count"]
        + report["spatial_skipped_pair_count"]
        == report["total_pair_count"]
    )
    assert altitude_bands_overlap(413.7, 423.7, 379.7, 385.8, 10.0) is False
    assert altitude_bands_overlap(413.7, 423.7, 413.7, 423.7, 10.0) is True

    # Check per-group provenance and explicit note that snapshot fallback covers only stations
    gp_by_name = {gp["group"]: gp for gp in report["group_provenance"]}
    assert gp_by_name["stations"]["source_mode"] == "live"
    assert gp_by_name["visual"]["source_mode"] == "live"
    assert gp_by_name["active"]["source_mode"] == "snapshot_fallback"
    assert gp_by_name["active"]["snapshot_covers_group"] is False
    assert "only the stations group" in gp_by_name["active"]["fallback_reason"].lower()
    assert SNAPSHOT_ONLY_STATIONS_NOTE in (report["snapshot_coverage_note"] or "")

    # Verify deduplication kept the newer epoch for ISS (25544) from group "visual"
    objs = loop.objects(now=now)["objects"]
    iss_obj = next(o for o in objs if o["norad_id"] == 25544)
    assert iss_obj["epoch"].startswith("2026-11-12T12:00:00")
    assert iss_obj["group"] == "visual"
    assert all(o["perigee_altitude_km"] < 2000.0 for o in objs)

    # Now test full fallback when only non-stations groups are configured and live fetch fails
    monkeypatch.setenv("ORBITGUARD_LOOP_GROUPS", "active,starlink")
    loop_fb = TrackingLoop(
        tmp_path / "full_fb",
        transport=lambda url: HttpResult(504, b"egress timeout", url),
        live_fetch=True,
    )
    fb_report = loop_fb.run_once(now=now)
    assert fb_report["source_mode"] == "snapshot_fallback"
    assert "only the stations group" in (fb_report["fallback_reason"] or "").lower()
    assert SNAPSHOT_ONLY_STATIONS_NOTE in (fb_report["snapshot_coverage_note"] or "")
    assert len(fb_report["group_provenance"]) == 2
    assert all(gp["snapshot_covers_group"] is False for gp in fb_report["group_provenance"])


def test_memory_across_iterations_and_pruning(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ORBITGUARD_LOOP_LIVE_FETCH", "0")
    t0 = datetime(2026, 11, 12, 16, 0, 0, tzinfo=timezone.utc)

    loop = TrackingLoop(tmp_path, step_s=30.0, keep_iterations=2)

    # Iteration 1: fresh propagation of all 19 objects
    r1 = loop.run_once(now=t0)
    assert r1["iteration_id"] == 1
    assert r1["propagated_count"] == 19
    assert r1["reused_count"] == 0

    # Iteration 2: 60s later (still well within the ~697s horizon) -> skips re-propagation
    t1 = t0 + timedelta(seconds=60)
    r2 = loop.run_once(now=t1)
    assert r2["iteration_id"] == 2
    assert r2["propagated_count"] == 0
    assert r2["reused_count"] == 19
    assert r2["conjunction_count"] == r1["conjunction_count"]
    assert r2["co_located_count"] == r1["co_located_count"]

    # Iteration 3: 1200s later (past the ~697s horizon) -> horizon no longer covers now, re-propagates all 19
    t2 = t0 + timedelta(seconds=1200)
    r3 = loop.run_once(now=t2)
    assert r3["iteration_id"] == 3
    assert r3["propagated_count"] == 19
    assert r3["reused_count"] == 0

    # Because keep_iterations=2, iteration 1 must have been pruned while iterations 2 and 3 remain
    status = loop.status(now=t2)
    assert status["iteration_count"] == 3
    assert status["retained_iteration_count"] == 2
    assert status["db_row_counts"]["iterations"] == 2
    assert status["db_row_counts"]["element_sets"] == 2 * 19

    with sqlite3.connect(str(tmp_path / "tracking_loop.sqlite")) as conn:
        ids = [r[0] for r in conn.execute("SELECT id FROM iterations ORDER BY id ASC").fetchall()]
        assert ids == [2, 3]
        orphan_states = conn.execute("SELECT COUNT(*) FROM propagated_states WHERE iteration_id = 1").fetchone()[0]
        assert orphan_states == 0
        orphan_coloc = conn.execute("SELECT COUNT(*) FROM co_located_pairs WHERE iteration_id = 1").fetchone()[0]
        assert orphan_coloc == 0


def test_live_fetch_failure_falls_back_to_snapshot_with_reason_and_age(tmp_path: Path):
    calls: list[str] = []

    def failing_transport(url: str) -> HttpResult:
        calls.append(url)
        return HttpResult(status=504, body=b"CelesTrak egress timed out after 8s", url=url)

    now = STATIONS_RETRIEVED_DT + timedelta(hours=2)
    loop = TrackingLoop(
        tmp_path,
        transport=failing_transport,
        live_fetch=True,
        fetch_timeout_s=4.0,
    )
    report = loop.run_once(now=now)

    assert len(calls) == len(DEFAULT_LOOP_GROUPS) == 6
    assert report["status"] == "ok"
    assert report["source_mode"] == "snapshot_fallback"
    assert report["source_name"] == "celestrak_stations_snapshot"
    assert "HTTP 504" in (report["fallback_reason"] or "")
    assert "timed out" in (report["fallback_reason"] or "")
    assert report["retrieved_at"] == STATIONS_RETRIEVED_AT
    assert report["snapshot_age_s"] == pytest.approx(2 * 3600.0)
    assert report["data_age_s"] == pytest.approx(2 * 3600.0)
    assert report["provenance"]["live"] is False
    assert report["provenance"]["snapshot"] is True


def test_iteration_error_is_recorded_and_loop_recovers(tmp_path: Path):
    class FailingSource(ObservationSource):
        name = "broken_source"
        kind = "gp_element_set"
        should_fail = True

        def status(self) -> SourceStatus:
            return SourceStatus(name=self.name, kind=self.kind, status="error" if self.should_fail else "ready")

        def fetch(self, now: Optional[datetime] = None) -> ObservationBatch:
            if self.should_fail:
                raise RuntimeError("simulated upstream failure")
            store = CatalogStore(tmp_path / "catalog.json")
            return CatalogElementSetSource(store, live_fetch=False).fetch(now=now)

    source = FailingSource()
    loop = TrackingLoop(tmp_path, sources=[source, LidarObservationSource(), VisionObservationSource()])

    err_report = loop.run_once(now=datetime(2026, 11, 12, 16, 0, 0, tzinfo=timezone.utc))
    assert err_report["status"] == "error"
    assert "simulated upstream failure" in err_report["error"]

    st = loop.status()
    assert st["iteration_count"] == 1
    assert "simulated upstream failure" in (st["last_error"] or "")

    # Next iteration succeeds and clears last_error
    source.should_fail = False
    ok_report = loop.run_once(now=datetime(2026, 11, 12, 16, 5, 0, tzinfo=timezone.utc))
    assert ok_report["status"] == "ok"
    assert ok_report["object_count"] == 19
    assert loop.status()["last_error"] is None


def test_loop_api_endpoints_provenance_and_no_probability(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ORBITGUARD_LOOP_LIVE_FETCH", "0")
    monkeypatch.setenv("ORBITGUARD_LOOP_ENABLED", "0")
    fixed_now = datetime(2026, 11, 12, 16, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr("orbitguard.monitor.api._now", lambda: fixed_now)
    client, loop = _client_with_loop(tmp_path, step_s=30.0, threshold_km=500.0)

    # 1. Initial status before any iteration
    status_before = client.get("/api/orbitguard/loop/status").json()
    assert status_before["running"] is False
    assert status_before["enabled"] is False
    assert status_before["iteration_count"] == 0
    assert status_before["db_path"].endswith("tracking_loop.sqlite")
    source_map = {s["name"]: s for s in status_before["sources"]}
    assert source_map["lidar"]["status"] == "not_connected"
    assert source_map["xvision"]["status"] == "not_connected"

    # 2. Trigger synchronous iteration via POST /api/orbitguard/loop/run-once
    t_start = time.perf_counter()
    run_res = client.post("/api/orbitguard/loop/run-once")
    elapsed_s = time.perf_counter() - t_start
    assert run_res.status_code == 200
    assert elapsed_s < 5.0
    run_body = run_res.json()
    assert run_body["status"] == "ok"
    assert run_body["iteration_id"] == 1
    assert run_body["source_mode"] == "snapshot_fallback"
    assert run_body["object_count"] == 19
    assert run_body["conjunction_count"] == 8
    assert run_body["co_located_count"] == 38
    assert run_body["label"] == SCREENING_LABEL

    # 3. GET /api/orbitguard/loop/status after iteration
    status_after = client.get("/api/orbitguard/loop/status").json()
    assert status_after["iteration_count"] == 1
    assert status_after["last_started_at"] is not None
    assert status_after["last_finished_at"] is not None
    assert status_after["source_mode"] == "snapshot_fallback"
    assert status_after["conjunction_count"] == 8
    assert status_after["co_located_count"] == 38
    assert status_after["screened_pair_count"] + status_after["skipped_pair_count"] == 171
    assert (
        status_after["radial_skipped_pair_count"] + status_after["spatial_skipped_pair_count"]
        == status_after["skipped_pair_count"]
    )
    assert status_after["provenance"]["source_name"] == "celestrak_stations_snapshot"
    assert status_after["provenance"]["retrieved_at"] == STATIONS_RETRIEVED_AT
    assert status_after["provenance"]["snapshot"] is True
    assert status_after["provenance"]["live"] is False
    assert status_after["provenance"]["snapshot_age_s"] is not None
    assert status_after["provenance"]["data_age_s"] is not None
    assert status_after["db_row_counts"]["iterations"] == 1
    assert status_after["db_row_counts"]["element_sets"] == 19
    assert status_after["db_row_counts"]["propagated_states"] > 0
    assert status_after["db_row_counts"]["conjunction_screenings"] == 8
    assert status_after["db_row_counts"]["co_located_pairs"] == 38

    # 4. GET /api/orbitguard/loop/objects
    objects_res = client.get("/api/orbitguard/loop/objects")
    assert objects_res.status_code == 200
    objects_body = objects_res.json()
    assert objects_body["iteration_id"] == 1
    assert objects_body["count"] == 19
    assert objects_body["source_mode"] == "snapshot_fallback"
    iss = next(o for o in objects_body["objects"] if o["norad_id"] == 25544)
    assert iss["name"] == "ISS (ZARYA)"
    assert iss["epoch"].startswith("2026-11-12")
    assert iss["epoch_age_hours"] is not None
    assert iss["perigee_altitude_km"] < 2000.0
    assert iss["source"] == "celestrak_stations_snapshot"
    assert iss["retrieved_at"] == STATIONS_RETRIEVED_AT
    assert iss["propagated_horizon"]["duration_s"] == pytest.approx(iss["period_s"] / 8.0)
    assert iss["propagated_horizon"]["sample_count"] > 0
    assert iss["latest_state"]["frame"] == "TEME"

    # 5. GET /api/orbitguard/loop/conjunctions
    conj_res = client.get("/api/orbitguard/loop/conjunctions")
    assert conj_res.status_code == 200
    conj_body = conj_res.json()
    assert conj_body["iteration_id"] == 1
    assert conj_body["label"] == SCREENING_LABEL
    assert conj_body["source_mode"] == "snapshot_fallback"
    assert conj_body["provenance"]["retrieved_at"] == STATIONS_RETRIEVED_AT
    assert conj_body["count"] == 8
    assert conj_body["co_located_count"] == 38
    assert len(conj_body["co_located"]) == 38
    assert conj_body["co_located"][0]["reason"] == COLOCATED_REASON
    first_conj = conj_body["conjunctions"][0]
    assert first_conj["label"] == SCREENING_LABEL
    assert 0.0 < first_conj["min_miss_km"] <= 500.0
    assert first_conj["tca"].endswith("Z")
    assert first_conj["frame"] == "TEME"
    assert first_conj["human_review_required"] is True

    # 6. Verify no probability key or value is output anywhere across loop responses
    _assert_no_probability_value(run_body)
    _assert_no_probability_value(status_after)
    _assert_no_probability_value(objects_body)
    _assert_no_probability_value(conj_body)


def test_background_thread_does_not_block_fastapi_requests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Confirm the background thread never blocks the FastAPI event loop while an iteration is running."""
    monkeypatch.setenv("ORBITGUARD_LOOP_ENABLED", "1")
    fetch_entered = threading.Event()
    release_fetch = threading.Event()

    class SlowSource(ObservationSource):
        name = "slow_catalog"
        kind = "gp_element_set"

        def status(self) -> SourceStatus:
            return SourceStatus(name=self.name, kind=self.kind, status="ready", connected=True)

        def fetch(self, now: Optional[datetime] = None) -> ObservationBatch:
            fetch_entered.set()
            release_fetch.wait(timeout=5.0)
            store = CatalogStore(tmp_path / "catalog.json")
            return CatalogElementSetSource(store, live_fetch=False).fetch(now=now)

    service = MonitorService(
        tmp_path,
        transport=lambda url: HttpResult(504, b"disabled", url),
    )
    loop = TrackingLoop(
        tmp_path,
        store=service.store,
        sources=[SlowSource(), LidarObservationSource(), VisionObservationSource()],
    )
    service.loop = loop
    app = FastAPI()
    app.include_router(build_test_router(service, loop=loop))
    client = TestClient(app)

    try:
        assert loop.start() is True
        assert fetch_entered.wait(timeout=2.0) is True
        # While the background thread is blocked inside fetch(), API endpoints must respond immediately
        t0 = time.perf_counter()
        status_res = client.get("/api/orbitguard/loop/status")
        elapsed = time.perf_counter() - t0
        assert status_res.status_code == 200
        assert elapsed < 0.5
        assert status_res.json()["running"] is True
    finally:
        release_fetch.set()
        loop.stop(timeout_s=3.0)


def test_lifespan_respects_orbitguard_loop_enabled_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ORBITGUARD_CACHE_DIR", str(tmp_path))
    monkeypatch.setenv("ORBITGUARD_LOOP_LIVE_FETCH", "0")
    monkeypatch.setenv("ORBITGUARD_LOOP_ENABLED", "0")

    with TestClient(main_app) as client:
        st = client.get("/api/orbitguard/loop/status").json()
        assert st["enabled"] is False
        assert st["running"] is False
        assert st["iteration_count"] == 0


def test_partial_cache_reuse_when_one_object_updates_and_sqlite_cache_reload(tmp_path: Path):
    base_records = json.loads(Path("data/gp/stations.json").read_text(encoding="utf-8"))
    current_payload = list(base_records)

    def mock_transport(url: str) -> HttpResult:
        return HttpResult(status=200, body=json.dumps(current_payload).encode("utf-8"), url=url)

    t0 = datetime(2026, 11, 12, 16, 0, 0, tzinfo=timezone.utc)
    loop1 = TrackingLoop(tmp_path, transport=mock_transport, live_fetch=True, step_s=30.0)
    r1 = loop1.run_once(now=t0)
    assert r1["source_mode"] == "live"
    assert r1["propagated_count"] == 19
    assert r1["reused_count"] == 0

    # Create a new TrackingLoop instance backed by the same SQLite DB; modify 1 object's epoch
    updated_first = dict(base_records[0])
    updated_first["EPOCH"] = "2026-11-12T06:00:00.000000"
    current_payload = [updated_first] + list(base_records[1:])

    loop2 = TrackingLoop(tmp_path, transport=mock_transport, live_fetch=True, step_s=30.0)
    r2 = loop2.run_once(now=t0 + timedelta(seconds=30))
    assert r2["source_mode"] == "live"
    assert r2["propagated_count"] == 1
    assert r2["reused_count"] == 18
