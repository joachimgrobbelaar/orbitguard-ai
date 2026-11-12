"""Tests for GitHubSnapshotSource, multi-group CelesTrak snapshotter, and loop cap/source wiring."""

from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
import importlib.util
import io
import json
from pathlib import Path
from typing import Any, Mapping, Optional
import urllib.error
import urllib.request

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from orbitguard.monitor.api import build_test_router
from orbitguard.monitor.ingest import CatalogStore, HttpResult
from orbitguard.monitor.loop import (
    DEFAULT_LOOP_MAX_OBJECTS,
    TrackingLoop,
    altitude_bands_overlap,
)
from orbitguard.monitor.sensors import (
    DEFAULT_GITHUB_BRANCH,
    DEFAULT_GITHUB_OWNER,
    DEFAULT_GITHUB_REPO,
    DEFAULT_LOOP_GROUPS,
    GITHUB_UNCONFIGURED_REASON,
    CatalogElementSetSource,
    GitHubSnapshotSource,
    configured_loop_groups,
)
from orbitguard.monitor.service import MonitorService

REPO_ROOT = Path(__file__).resolve().parents[1]
STATIONS_RECORDS = json.loads((REPO_ROOT / "data" / "gp" / "stations.json").read_text(encoding="utf-8"))
STATIONS_META = json.loads((REPO_ROOT / "data" / "gp" / "stations.meta.json").read_text(encoding="utf-8"))


def _b64_contents_response(payload: Any, url: str) -> HttpResult:
    """Encode ``payload`` as a GitHub REST contents API JSON response with base64 ``content``."""
    raw_bytes = json.dumps(payload, indent=2).encode("utf-8")
    # GitHub contents API wraps base64 lines at 60 chars with newlines
    b64_raw = base64.b64encode(raw_bytes).decode("ascii")
    b64_with_newlines = "\n".join(b64_raw[i : i + 60] for i in range(0, len(b64_raw), 60)) + "\n"
    envelope = {
        "name": url.rsplit("/", 1)[-1].split("?")[0],
        "encoding": "base64",
        "size": len(raw_bytes),
        "content": b64_with_newlines,
    }
    return HttpResult(status=200, body=json.dumps(envelope).encode("utf-8"), url=url)


def _make_omm(norad_id: int, name: str, *, mean_motion: float = 15.2, epoch: str = "2026-11-12T08:00:00.000000") -> dict[str, Any]:
    base = dict(STATIONS_RECORDS[0])
    base["NORAD_CAT_ID"] = norad_id
    base["OBJECT_NAME"] = name
    base["MEAN_MOTION"] = mean_motion
    base["ECCENTRICITY"] = 0.001
    base["EPOCH"] = epoch
    return base


def _load_snapshot_script():
    script_path = REPO_ROOT / "scripts" / "fetch_celestrak_snapshot.py"
    spec = importlib.util.spec_from_file_location("fetch_celestrak_snapshot", script_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_default_loop_groups_and_max_objects_constants(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("ORBITGUARD_LOOP_GROUPS", raising=False)
    expected_groups = [
        "stations",
        "active",
        "cosmos-1408-debris",
        "cosmos-2251-debris",
        "fengyun-1c-debris",
        "iridium-33-debris",
    ]
    assert list(DEFAULT_LOOP_GROUPS) == expected_groups
    assert configured_loop_groups() == expected_groups
    assert DEFAULT_LOOP_MAX_OBJECTS == 2000


def test_github_snapshot_source_token_path_contents_api_and_secret_redaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    secret_token = "ghp_SuperSecretOrbitGuardTestToken987654321"
    monkeypatch.setenv("ORBITGUARD_GITHUB_TOKEN", secret_token)
    monkeypatch.setenv("ORBITGUARD_LOOP_GROUPS", "stations,cosmos-1408-debris")

    stations_subset = STATIONS_RECORDS[:2]
    debris_record = _make_omm(49863, "COSMOS 1408 DEB", mean_motion=15.15)
    t_snap_stations = "2026-11-12T14:00:00Z"
    t_snap_debris = "2026-11-12T13:30:00Z"
    now = datetime(2026, 11, 12, 16, 0, 0, tzinfo=timezone.utc)

    stations_meta = {
        "group": "stations",
        "source_name": "celestrak_stations_snapshot",
        "source_url": "https://celestrak.org/NORAD/elements/gp.php?GROUP=stations&FORMAT=JSON",
        "retrieved_at": t_snap_stations,
        "retrieved_by": "github-actions",
        "object_count": len(stations_subset),
        "live_download": False,
        "note": "Snapshot of one CelesTrak stations-group download.",
    }
    debris_meta = {
        "group": "cosmos-1408-debris",
        "source_name": "celestrak_cosmos_1408_debris_snapshot",
        "source_url": "https://celestrak.org/NORAD/elements/gp.php?GROUP=cosmos-1408-debris&FORMAT=JSON",
        "retrieved_at": t_snap_debris,
        "retrieved_by": "github-actions",
        "object_count": 1,
        "live_download": False,
        "note": "Snapshot of one CelesTrak cosmos-1408-debris-group download.",
    }

    recorded_calls: list[tuple[str, dict[str, str]]] = []

    def mock_transport(url: str, headers: Optional[Mapping[str, str]] = None) -> HttpResult:
        hdrs = dict(headers or {})
        recorded_calls.append((url, hdrs))
        if "contents/data/gp/stations.json" in url:
            return _b64_contents_response(stations_subset, url)
        if "contents/data/gp/stations.meta.json" in url:
            return _b64_contents_response(stations_meta, url)
        if "contents/data/gp/cosmos-1408-debris.json" in url:
            return _b64_contents_response([debris_record], url)
        if "contents/data/gp/cosmos-1408-debris.meta.json" in url:
            return _b64_contents_response(debris_meta, url)
        return HttpResult(status=404, body=b"not found", url=url)

    store = CatalogStore(tmp_path / "catalog.json")
    source = GitHubSnapshotSource(store, transport=mock_transport)

    st_before = source.status()
    assert st_before["configured"] is True
    assert st_before["auth_mode"] == "token"
    assert st_before["token_configured"] is True
    assert st_before["owner"] == DEFAULT_GITHUB_OWNER
    assert st_before["repo"] == DEFAULT_GITHUB_REPO
    assert st_before["branch"] == DEFAULT_GITHUB_BRANCH
    assert secret_token not in json.dumps(st_before)

    batch = source.fetch(now=now)

    assert len(recorded_calls) == 4
    for url, hdrs in recorded_calls:
        assert url.startswith(
            f"https://api.github.com/repos/{DEFAULT_GITHUB_OWNER}/{DEFAULT_GITHUB_REPO}/contents/data/gp/"
        )
        assert url.endswith(f"?ref={DEFAULT_GITHUB_BRANCH}")
        assert hdrs.get("Authorization") == f"Bearer {secret_token}"
        assert "OrbitGuard/0.1" in hdrs.get("User-Agent", "")
        assert "github" in hdrs.get("Accept", "").lower() or "json" in hdrs.get("Accept", "").lower()

    assert len(batch.records) == 3
    assert batch.source_mode == "github_snapshot"
    assert batch.origin_flag == "snapshot"
    assert batch.live is False
    assert batch.snapshot is True
    # Batch retrieved_at and age use the oldest committed snapshot retrieved_at (13:30Z -> 2.5h = 9000s),
    # NOT 0s from the moment of the GitHub HTTP fetch.
    assert batch.retrieved_at == t_snap_debris
    assert batch.snapshot_age_s == pytest.approx(2.5 * 3600.0)
    assert batch.data_age_s == pytest.approx(2.5 * 3600.0)
    assert batch.fetched_at == "2026-11-12T16:00:00Z"

    gp_by_group = {gp["group"]: gp for gp in batch.group_provenance}
    assert gp_by_group["stations"]["source_url"] == stations_meta["source_url"]
    assert gp_by_group["stations"]["retrieved_at"] == t_snap_stations
    assert gp_by_group["stations"]["snapshot_age_s"] == pytest.approx(2.0 * 3600.0)
    assert gp_by_group["stations"]["object_count"] == 2
    assert "api.github.com" in gp_by_group["stations"]["github_url"]

    assert gp_by_group["cosmos-1408-debris"]["source_url"] == debris_meta["source_url"]
    assert gp_by_group["cosmos-1408-debris"]["retrieved_at"] == t_snap_debris
    assert gp_by_group["cosmos-1408-debris"]["snapshot_age_s"] == pytest.approx(2.5 * 3600.0)
    assert gp_by_group["cosmos-1408-debris"]["object_count"] == 1

    st_after = source.status()
    assert st_after["source_mode"] == "github_snapshot"
    assert secret_token not in json.dumps(st_after)
    assert secret_token not in json.dumps(batch.to_dict())


def test_github_snapshot_source_public_raw_path_no_auth_header(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.delenv("ORBITGUARD_GITHUB_TOKEN", raising=False)
    monkeypatch.setenv("ORBITGUARD_GITHUB_PUBLIC", "1")
    monkeypatch.setenv("ORBITGUARD_GITHUB_OWNER", "custom-owner")
    monkeypatch.setenv("ORBITGUARD_GITHUB_REPO", "custom-repo")
    monkeypatch.setenv("ORBITGUARD_GITHUB_BRANCH", "main")
    monkeypatch.setenv("ORBITGUARD_LOOP_GROUPS", "stations")

    stations_subset = STATIONS_RECORDS[:3]
    meta = dict(STATIONS_META)
    meta["retrieved_at"] = "2026-11-12T15:00:00Z"
    meta["object_count"] = 3
    now = datetime(2026, 11, 12, 16, 0, 0, tzinfo=timezone.utc)

    recorded_calls: list[tuple[str, dict[str, str]]] = []

    def raw_transport(url: str, headers: Optional[Mapping[str, str]] = None) -> HttpResult:
        hdrs = dict(headers or {})
        recorded_calls.append((url, hdrs))
        if url.endswith("/data/gp/stations.json"):
            return HttpResult(status=200, body=json.dumps(stations_subset).encode("utf-8"), url=url)
        if url.endswith("/data/gp/stations.meta.json"):
            return HttpResult(status=200, body=json.dumps(meta).encode("utf-8"), url=url)
        return HttpResult(status=404, body=b"not found", url=url)

    store = CatalogStore(tmp_path / "catalog.json")
    source = GitHubSnapshotSource(store, transport=raw_transport)
    st = source.status()
    assert st["configured"] is True
    assert st["auth_mode"] == "public_raw"
    assert st["token_configured"] is False
    assert st["public_raw_enabled"] is True
    assert st["owner"] == "custom-owner"
    assert st["repo"] == "custom-repo"
    assert st["branch"] == "main"

    batch = source.fetch(now=now)
    assert len(recorded_calls) == 2
    assert recorded_calls[0][0] == "https://raw.githubusercontent.com/custom-owner/custom-repo/main/data/gp/stations.json"
    assert recorded_calls[1][0] == "https://raw.githubusercontent.com/custom-owner/custom-repo/main/data/gp/stations.meta.json"
    for _url, hdrs in recorded_calls:
        assert "Authorization" not in hdrs
        assert "OrbitGuard/0.1" in hdrs.get("User-Agent", "")

    assert len(batch.records) == 3
    assert batch.source_mode == "github_snapshot"
    assert batch.origin_flag == "snapshot"
    assert batch.live is False
    assert batch.snapshot is True
    assert batch.retrieved_at == "2026-11-12T15:00:00Z"
    assert batch.snapshot_age_s == pytest.approx(3600.0)
    assert batch.data_age_s == pytest.approx(3600.0)


def test_github_snapshot_source_unconfigured_makes_no_http_calls_and_falls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.delenv("ORBITGUARD_GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("ORBITGUARD_GITHUB_PUBLIC", raising=False)
    monkeypatch.setenv("ORBITGUARD_LOOP_GROUPS", "stations,active")

    calls: list[str] = []

    def forbidden_transport(url: str, headers: Optional[Mapping[str, str]] = None) -> HttpResult:
        calls.append(url)
        raise AssertionError("Network must not be called when GitHubSnapshotSource is unconfigured")

    now = datetime(2026, 11, 12, 16, 23, 31, 784639, tzinfo=timezone.utc)
    store = CatalogStore(tmp_path / "catalog.json")
    source = GitHubSnapshotSource(store, transport=forbidden_transport)
    assert source.configured is False
    assert source.status()["auth_mode"] == "unconfigured"

    batch = source.fetch(now=now)
    assert calls == []
    assert batch.source_mode == "snapshot_fallback"
    assert batch.origin_flag == "snapshot"
    assert batch.live is False
    assert batch.snapshot is True
    assert GITHUB_UNCONFIGURED_REASON in (batch.fallback_reason or "")
    assert len(batch.records) == 19
    assert batch.retrieved_at == "2026-11-12T15:23:31.784639Z"
    assert batch.snapshot_age_s == pytest.approx(3600.0)
    assert batch.data_age_s == pytest.approx(3600.0)


def test_github_snapshot_source_total_failure_falls_back_to_bundled_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    secret = "ghp_NeverLeakOnError12345"
    monkeypatch.setenv("ORBITGUARD_GITHUB_TOKEN", secret)
    monkeypatch.setenv("ORBITGUARD_LOOP_GROUPS", "stations,active")

    def failing_transport(url: str, headers: Optional[Mapping[str, str]] = None) -> HttpResult:
        if "stations" in url:
            return HttpResult(status=504, body=f"timeout with {secret}".encode("utf-8"), url=url)
        return HttpResult(status=401, body=b"Bad credentials", url=url)

    now = datetime(2026, 11, 12, 17, 23, 31, 784639, tzinfo=timezone.utc)
    store = CatalogStore(tmp_path / "catalog.json")
    source = GitHubSnapshotSource(store, transport=failing_transport)
    batch = source.fetch(now=now)

    assert batch.source_mode == "snapshot_fallback"
    assert batch.live is False
    assert batch.snapshot is True
    assert len(batch.records) == 19
    assert "HTTP 504" in (batch.fallback_reason or "")
    assert "HTTP 401" in (batch.fallback_reason or "")
    assert batch.retrieved_at == "2026-11-12T15:23:31.784639Z"
    assert batch.snapshot_age_s == pytest.approx(7200.0)
    assert batch.data_age_s == pytest.approx(7200.0)
    assert secret not in json.dumps(batch.to_dict())
    assert secret not in json.dumps(source.status())


def test_github_snapshot_multi_group_dedup_and_partial_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("ORBITGUARD_GITHUB_PUBLIC", "1")
    monkeypatch.setenv("ORBITGUARD_LOOP_GROUPS", "stations,active,iridium-33-debris")

    # NORAD 25544 appears in both 'stations' (older epoch) and 'active' (newer epoch).
    iss_old = _make_omm(25544, "ISS (ZARYA)", epoch="2026-11-12T04:00:00.000000")
    css_rec = _make_omm(48274, "CSS (TIANHE)", epoch="2026-11-12T05:00:00.000000")
    iss_new = _make_omm(25544, "ISS (ZARYA)", epoch="2026-11-12T10:00:00.000000")
    active_only = _make_omm(43013, "NOAA 20", epoch="2026-11-12T09:00:00.000000")

    stations_meta = {
        "group": "stations",
        "source_name": "celestrak_stations_snapshot",
        "source_url": "https://celestrak.org/NORAD/elements/gp.php?GROUP=stations&FORMAT=JSON",
        "retrieved_at": "2026-11-12T14:00:00Z",
        "retrieved_by": "github-actions",
        "object_count": 2,
        "live_download": False,
    }
    active_meta = {
        "group": "active",
        "source_name": "celestrak_active_snapshot",
        "source_url": "https://celestrak.org/NORAD/elements/gp.php?GROUP=active&FORMAT=JSON",
        "retrieved_at": "2026-11-12T14:00:00Z",
        "retrieved_by": "github-actions",
        "object_count": 2,
        "live_download": False,
    }

    def partial_transport(url: str, headers: Optional[Mapping[str, str]] = None) -> HttpResult:
        if url.endswith("/data/gp/stations.json"):
            return HttpResult(200, json.dumps([iss_old, css_rec]).encode("utf-8"), url)
        if url.endswith("/data/gp/stations.meta.json"):
            return HttpResult(200, json.dumps(stations_meta).encode("utf-8"), url)
        if url.endswith("/data/gp/active.json"):
            return HttpResult(200, json.dumps([iss_new, active_only]).encode("utf-8"), url)
        if url.endswith("/data/gp/active.meta.json"):
            return HttpResult(200, json.dumps(active_meta).encode("utf-8"), url)
        # Simulate 404 for iridium-33-debris
        return HttpResult(404, b"404: Not Found", url)

    now = datetime(2026, 11, 12, 15, 0, 0, tzinfo=timezone.utc)
    store = CatalogStore(tmp_path / "catalog.json")
    source = GitHubSnapshotSource(store, transport=partial_transport)
    batch = source.fetch(now=now)

    # Must NOT fall back to bundled stations-only: must return the 3 unique records from stations + active
    assert batch.source_mode == "github_snapshot"
    assert batch.live is False
    assert batch.snapshot is True
    assert batch.deduplicated_count == 1
    assert len(batch.records) == 3
    by_norad = {int(r["NORAD_CAT_ID"]): r for r in batch.records}
    assert set(by_norad.keys()) == {25544, 48274, 43013}
    # Newer epoch from 'active' won deduplication for NORAD 25544
    assert by_norad[25544]["EPOCH"] == "2026-11-12T10:00:00.000000"

    gp_map = {gp["group"]: gp for gp in batch.group_provenance}
    assert gp_map["stations"]["status"] == "ok"
    assert gp_map["stations"]["object_count"] == 2
    assert gp_map["active"]["status"] == "ok"
    assert gp_map["active"]["object_count"] == 2
    assert gp_map["iridium-33-debris"]["status"] == "error"
    assert gp_map["iridium-33-debris"]["object_count"] == 0
    assert "HTTP 404" in (gp_map["iridium-33-debris"]["fallback_reason"] or "")
    assert "iridium-33-debris" in (batch.fallback_reason or "")


def test_loop_selects_github_source_caps_objects_and_reports_in_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    # Configure public GitHub snapshot source, 3 groups, and a small cap of 4 objects
    monkeypatch.setenv("ORBITGUARD_GITHUB_PUBLIC", "1")
    monkeypatch.setenv("ORBITGUARD_LOOP_GROUPS", "stations,cosmos-1408-debris,active")
    monkeypatch.setenv("ORBITGUARD_LOOP_MAX_OBJECTS", "4")

    # 2 stations LEO objects (NORAD 25544 at ~420 km, NORAD 48274 at ~380 km)
    s1 = STATIONS_RECORDS[0]  # 25544
    s2 = STATIONS_RECORDS[2]  # 48274
    # 3 cosmos-1408-debris LEO objects at ~1100 km (mean_motion=13.4 rev/day) + 1 MEO object (filtered before cap)
    d1 = _make_omm(50003, "COSMOS 1408 DEB C", mean_motion=13.4)
    d2 = _make_omm(50001, "COSMOS 1408 DEB A", mean_motion=13.4)
    d3 = _make_omm(50002, "COSMOS 1408 DEB B", mean_motion=13.4)
    meo = _make_omm(99999, "MEO OBJECT", mean_motion=2.0)  # non-LEO, filtered before cap
    # 1 active LEO object (should be dropped by cap=4 because stations (2) + cosmos-1408-debris (3) = 5 > 4)
    a1 = _make_omm(40001, "ACTIVE SAT 1", mean_motion=15.0)

    meta_template = {
        "source_url": "https://celestrak.org/NORAD/elements/gp.php?GROUP=stations&FORMAT=JSON",
        "retrieved_at": "2026-11-12T15:00:00Z",
        "retrieved_by": "github-actions",
        "live_download": False,
    }

    celestrak_called = False

    def github_transport(url: str, headers: Optional[Mapping[str, str]] = None) -> HttpResult:
        nonlocal celestrak_called
        if "celestrak.org" in url:
            celestrak_called = True
            raise AssertionError("Live CelesTrak must not be called when GitHubSnapshotSource is configured")
        if url.endswith("/data/gp/stations.json"):
            return HttpResult(200, json.dumps([s1, s2]).encode("utf-8"), url)
        if url.endswith("/data/gp/stations.meta.json"):
            return HttpResult(200, json.dumps({**meta_template, "group": "stations", "object_count": 2}).encode("utf-8"), url)
        if url.endswith("/data/gp/cosmos-1408-debris.json"):
            return HttpResult(200, json.dumps([d1, d2, d3, meo]).encode("utf-8"), url)
        if url.endswith("/data/gp/cosmos-1408-debris.meta.json"):
            return HttpResult(
                200,
                json.dumps(
                    {
                        **meta_template,
                        "group": "cosmos-1408-debris",
                        "source_url": "https://celestrak.org/NORAD/elements/gp.php?GROUP=cosmos-1408-debris&FORMAT=JSON",
                        "object_count": 4,
                    }
                ).encode("utf-8"),
                url,
            )
        if url.endswith("/data/gp/active.json"):
            return HttpResult(200, json.dumps([a1]).encode("utf-8"), url)
        if url.endswith("/data/gp/active.meta.json"):
            return HttpResult(
                200,
                json.dumps(
                    {
                        **meta_template,
                        "group": "active",
                        "source_url": "https://celestrak.org/NORAD/elements/gp.php?GROUP=active&FORMAT=JSON",
                        "object_count": 1,
                    }
                ).encode("utf-8"),
                url,
            )
        return HttpResult(404, b"not found", url)

    now = datetime(2026, 11, 12, 16, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr("orbitguard.monitor.api._now", lambda: now)

    service = MonitorService(tmp_path, transport=github_transport)
    loop = TrackingLoop(tmp_path, store=service.store, transport=github_transport, step_s=60.0)
    service.loop = loop
    app = FastAPI()
    app.include_router(build_test_router(service, loop=loop))
    client = TestClient(app)

    assert isinstance(loop.catalog_source, GitHubSnapshotSource)
    assert celestrak_called is False

    run_res = client.post("/api/orbitguard/loop/run-once").json()
    assert run_res["status"] == "ok"
    assert run_res["source_mode"] == "github_snapshot"
    assert run_res["catalog_source_class"] == "GitHubSnapshotSource"
    assert run_res["data_source_origin"] == "github"
    assert run_res["non_leo_filtered_count"] == 1
    assert run_res["objects_before_cap"] == 6  # 2 stations + 3 debris + 1 active (after 1 MEO filtered)
    assert run_res["objects_after_cap"] == 4
    assert run_res["object_count"] == 4
    assert run_res["objects_capped"] is True
    assert run_res["max_objects"] == 4
    assert run_res["objects_dropped_by_cap"] == 2
    assert "ORBITGUARD_LOOP_MAX_OBJECTS=4" in (run_res["cap_reason"] or "")

    # Deterministic policy: keep stations (25544, 48274) first, then cosmos-1408-debris by NORAD ascending (50001, 50002)
    objs_res = client.get("/api/orbitguard/loop/objects").json()
    tracked_norads = [o["norad_id"] for o in objs_res["objects"]]
    assert tracked_norads == [25544, 48274, 50001, 50002]

    # Spatial prefilter: stations (~380km and ~420km) do not overlap with debris (~1100km),
    # and 380km does not overlap with 420km at 10km threshold; only the pair (50001, 50002) overlaps!
    assert run_res["total_pair_count"] == 6  # 4 * 3 // 2
    assert run_res["screened_pair_count"] == 1
    assert run_res["skipped_pair_count"] == 5
    assert altitude_bands_overlap(414.0, 424.0, 1080.0, 1100.0, 10.0) is False

    # Verify /api/orbitguard/loop/status reports all required fields clearly
    st_res = client.get("/api/orbitguard/loop/status").json()
    assert st_res["iteration_count"] == 1
    assert st_res["source_mode"] == "github_snapshot"
    assert st_res["catalog_source_class"] == "GitHubSnapshotSource"
    assert st_res["data_source"]["class"] == "GitHubSnapshotSource"
    assert st_res["data_source"]["origin"] == "github"
    assert st_res["object_count"] == 4
    assert st_res["objects_capped"] is True
    assert st_res["max_objects"] == 4
    assert st_res["objects_before_cap"] == 6
    assert st_res["objects_after_cap"] == 4
    assert st_res["cap"]["objects_capped"] is True
    assert st_res["groups_requested"] == ["stations", "cosmos-1408-debris", "active"]
    assert st_res["groups_retrieved"] == ["stations", "cosmos-1408-debris", "active"]
    assert st_res["snapshot_age_s"] == pytest.approx(3600.0)
    assert st_res["data_age_s"] == pytest.approx(3600.0)


def test_loop_uses_catalog_element_set_source_when_github_not_configured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.delenv("ORBITGUARD_GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("ORBITGUARD_GITHUB_PUBLIC", raising=False)
    monkeypatch.setenv("ORBITGUARD_LOOP_LIVE_FETCH", "0")

    loop = TrackingLoop(tmp_path)
    assert isinstance(loop.catalog_source, CatalogElementSetSource)
    st = loop.status()
    assert st["catalog_source_class"] == "CatalogElementSetSource"
    assert st["data_source"]["origin"] == "bundled_fallback"


def test_snapshotter_script_per_group_interval_skip_and_fault_isolation(tmp_path: Path):
    mod = _load_snapshot_script()
    assert list(mod.GROUPS) == [
        "stations",
        "active",
        "cosmos-1408-debris",
        "cosmos-2251-debris",
        "fengyun-1c-debris",
        "iridium-33-debris",
    ]

    dest_dir = tmp_path / "gp"
    dest_dir.mkdir(parents=True)
    now = datetime(2026, 11, 12, 16, 0, 0, tzinfo=timezone.utc)

    # 1. 'stations' is fresh (30 minutes old < 2 hours) -> must NOT be requested
    fresh_retrieved_at = (now - timedelta(minutes=30)).isoformat().replace("+00:00", "Z")
    (dest_dir / "stations.json").write_text(json.dumps(STATIONS_RECORDS[:2]), encoding="utf-8")
    (dest_dir / "stations.meta.json").write_text(
        json.dumps(
            {
                "group": "stations",
                "source_name": "celestrak_stations_snapshot",
                "source_url": mod.CELESTRAK_STATIONS_URL,
                "retrieved_at": fresh_retrieved_at,
                "retrieved_by": "github-actions",
                "object_count": 2,
                "live_download": False,
                "note": mod.group_note("stations"),
            }
        ),
        encoding="utf-8",
    )

    # 2. 'active' is stale (3 hours old > 2 hours), and will fail with HTTP 503 -> existing file must remain untouched
    stale_retrieved_at = (now - timedelta(hours=3)).isoformat().replace("+00:00", "Z")
    existing_active_json = json.dumps([_make_omm(43013, "OLD ACTIVE")], indent=2) + "\n"
    (dest_dir / "active.json").write_text(existing_active_json, encoding="utf-8")
    (dest_dir / "active.meta.json").write_text(
        json.dumps(
            {
                "group": "active",
                "source_name": "celestrak_active_snapshot",
                "source_url": mod.group_url("active"),
                "retrieved_at": stale_retrieved_at,
                "retrieved_by": "github-actions",
                "object_count": 1,
                "live_download": False,
                "note": mod.group_note("active"),
            }
        ),
        encoding="utf-8",
    )

    # 3. 'cosmos-1408-debris' has no meta on disk -> must be requested and written
    debris_payload = [_make_omm(49863, "COSMOS 1408 DEB")]
    requested_urls: list[str] = []

    class _FakeResponse:
        def __init__(self, status: int, body: bytes):
            self.status = status
            self._body = body

        def read(self) -> bytes:
            return self._body

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_val, exc_tb):
            return False

    def fake_urlopen(request: urllib.request.Request, timeout: float = 45.0):
        url = request.full_url
        requested_urls.append(url)
        assert request.get_header("User-agent") == mod.USER_AGENT
        if "GROUP=active" in url:
            raise urllib.error.HTTPError(url, 503, "Service Unavailable", hdrs=None, fp=io.BytesIO(b"down"))
        if "GROUP=cosmos-1408-debris" in url:
            return _FakeResponse(200, json.dumps(debris_payload).encode("utf-8"))
        raise AssertionError(f"Unexpected URL requested: {url}")

    rc = mod.fetch_group_snapshots(
        groups=["stations", "active", "cosmos-1408-debris"],
        dest=dest_dir,
        now=now,
        urlopen=fake_urlopen,
    )
    assert rc == 0
    # 'stations' was skipped because fresh; 'active' and 'cosmos-1408-debris' were requested sequentially
    assert requested_urls == [
        "https://celestrak.org/NORAD/elements/gp.php?GROUP=active&FORMAT=JSON",
        "https://celestrak.org/NORAD/elements/gp.php?GROUP=cosmos-1408-debris&FORMAT=JSON",
    ]
    # 'active' failed so its existing file on disk was left untouched
    assert (dest_dir / "active.json").read_text(encoding="utf-8") == existing_active_json
    # 'cosmos-1408-debris' succeeded and wrote both .json and .meta.json
    written_debris = json.loads((dest_dir / "cosmos-1408-debris.json").read_text(encoding="utf-8"))
    written_meta = json.loads((dest_dir / "cosmos-1408-debris.meta.json").read_text(encoding="utf-8"))
    assert len(written_debris) == 1
    assert written_meta["group"] == "cosmos-1408-debris"
    assert written_meta["retrieved_at"] == "2026-11-12T16:00:00Z"
    assert written_meta["retrieved_by"] == "github-actions"
    assert written_meta["object_count"] == 1
    assert written_meta["live_download"] is False
    assert "did not contact CelesTrak" in written_meta["note"]

    # When all requested groups are fresh, exit 0 and make 0 HTTP calls
    requested_urls.clear()
    rc_all_fresh = mod.fetch_group_snapshots(
        groups=["stations", "cosmos-1408-debris"],
        dest=dest_dir,
        now=now + timedelta(minutes=10),
        urlopen=fake_urlopen,
    )
    assert rc_all_fresh == 0
    assert requested_urls == []

    # When a fetch is needed and every requested fetch fails, exit non-zero (1)
    rc_all_fail = mod.fetch_group_snapshots(
        groups=["active"],
        dest=dest_dir,
        now=now,
        urlopen=fake_urlopen,
    )
    assert rc_all_fail == 1
