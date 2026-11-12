"""Tests for the two-layer broad-phase conjunction pre-filter (radial sieve + 3D spatial hash)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import random
import time
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient
import numpy as np
import pytest

from orbitguard.monitor.api import build_test_router
from orbitguard.monitor.broadphase import (
    NEIGHBOR_OFFSETS_27,
    POSITIVE_NEIGHBOR_OFFSETS_13,
    altitude_bands_overlap,
    count_radial_skipped_pairs,
    resolve_grid_km,
    screen_tracks_broadphase,
    screen_tracks_bruteforce,
    spatial_hash_bins,
)
from orbitguard.monitor.ingest import HttpResult
from orbitguard.monitor.loop import (
    COLOCATED_REASON,
    DEFAULT_LOOP_MAX_OBJECTS,
    PRIORITY_DEBRIS_GROUPS,
    SCREENING_LABEL,
    TrackingLoop,
    _evaluate_pair_horizon,
    cap_sort_key,
)
from orbitguard.monitor.sensors import DEFAULT_LOOP_GROUPS
from orbitguard.monitor.service import MonitorService


def _make_synthetic_samples(
    positions_km: list[tuple[float, float, float]],
    velocities_km_s: list[tuple[float, float, float]],
    *,
    t0: datetime,
    step_s: float = 30.0,
) -> list[dict[str, Any]]:
    samples = []
    for idx, (pos, vel) in enumerate(zip(positions_km, velocities_km_s)):
        t_dt = t0 + timedelta(seconds=idx * step_s)
        samples.append(
            {
                "time": t_dt.isoformat().replace("+00:00", "Z"),
                "r_teme_km": [float(pos[0]), float(pos[1]), float(pos[2])],
                "v_teme_km_s": [float(vel[0]), float(vel[1]), float(vel[2])],
                "frame": "TEME",
            }
        )
    return samples


def test_neighbor_offsets_cover_all_27_cells():
    """Verify the 3x3x3 neighborhood offsets and 13 positive half-space offsets."""
    assert len(NEIGHBOR_OFFSETS_27) == 27
    assert (0, 0, 0) in NEIGHBOR_OFFSETS_27
    assert len(POSITIVE_NEIGHBOR_OFFSETS_13) == 13
    # Every non-zero offset in the 27-neighborhood must appear either in POSITIVE_NEIGHBOR_OFFSETS_13
    # or as the negation of one in POSITIVE_NEIGHBOR_OFFSETS_13, with no overlap.
    pos_set = set(POSITIVE_NEIGHBOR_OFFSETS_13)
    neg_set = {(-dx, -dy, -dz) for (dx, dy, dz) in POSITIVE_NEIGHBOR_OFFSETS_13}
    assert pos_set.isdisjoint(neg_set)
    assert pos_set | neg_set | {(0, 0, 0)} == set(NEIGHBOR_OFFSETS_27)


def test_bruteforce_equivalence_with_cell_boundary_and_corner_cases():
    """
    Critical test: generate synthetic tracks with random trajectories PLUS explicit pairs
    straddling cell faces, cell edges, and 3D cell corners, as well as co-located pairs
    and pairs that start close and later drift apart. Verify exact equivalence between
    the brute-force all-pairs screener and the spatial-hash broad-phase screener.
    """
    rng = random.Random(20261112)
    t0 = datetime(2026, 11, 12, 16, 0, 0, tzinfo=timezone.utc)
    n_steps = 12
    step_s = 30.0
    threshold_km = 10.0
    colocated_km = 5.0
    colocated_rel_speed_km_s = 0.01

    tracks_by_norad: dict[int, list[dict[str, Any]]] = {}
    altitude_bands_by_norad: dict[int, tuple[float, float]] = {}
    names_by_norad: dict[int, str] = {}
    next_id = 1000

    def add_track(
        pos_list: list[tuple[float, float, float]],
        vel_list: list[tuple[float, float, float]],
        *,
        perigee_km: float = 550.0,
        apogee_km: float = 560.0,
    ) -> int:
        nonlocal next_id
        nid = next_id
        next_id += 1
        tracks_by_norad[nid] = _make_synthetic_samples(pos_list, vel_list, t0=t0, step_s=step_s)
        altitude_bands_by_norad[nid] = (perigee_km, apogee_km)
        names_by_norad[nid] = f"SYNTH-{nid}"
        return nid

    # 1. Explicit pair straddling a cell FACE at x = 7000.0 km (cells 699 and 700 for 10 km grid)
    # Distance = 0.2 km (<= 10 km), high relative speed (not co-located)
    face_a_pos = [(7000.0 - 0.1, 100.0 + k * 5.0, 200.0) for k in range(n_steps)]
    face_b_pos = [(7000.0 + 0.1, 100.0 + (n_steps - k) * 5.0, 200.0) for k in range(n_steps)]
    face_a_id = add_track(face_a_pos, [(0.0, 0.5, 0.0)] * n_steps)
    face_b_id = add_track(face_b_pos, [(0.0, -0.5, 0.0)] * n_steps)

    # 2. Explicit pair straddling a 3D cell CORNER at (-6900.0, 1200.0, -3400.0)
    # Object A is in cell (-691, 119, -341), Object B is in cell (-690, 120, -340) — diagonal corner neighbors!
    # At step 5, they sit at (-6904.0, 1196.0, -3404.0) and (-6899.0, 1201.0, -3399.0)
    # -> separation = sqrt(5^2 + 5^2 + 5^2) = sqrt(75) = 8.66025 km <= 10.0 km!
    corner_a_pos = []
    corner_b_pos = []
    for k in range(n_steps):
        if k == 5:
            corner_a_pos.append((-6904.0, 1196.0, -3404.0))
            corner_b_pos.append((-6899.0, 1201.0, -3399.0))
        else:
            corner_a_pos.append((-6904.0 - (k - 5) * 20.0, 1196.0, -3404.0))
            corner_b_pos.append((-6899.0 + (k - 5) * 20.0, 1201.0, -3399.0))
    corner_a_id = add_track(corner_a_pos, [(1.0, 0.0, 0.0)] * n_steps)
    corner_b_id = add_track(corner_b_pos, [(-1.0, 0.0, 0.0)] * n_steps)

    # 3. True co-located pair straddling a cell boundary throughout the entire horizon
    # Distance = 0.4 km <= 5.0 km at all steps, relative speed = 0.002 km/s <= 0.01 km/s
    coloc_a_pos = [(6800.0 - 0.2, 50.0 + k * 7.5, -100.0) for k in range(n_steps)]
    coloc_b_pos = [(6800.0 + 0.2, 50.0 + k * 7.5, -100.0) for k in range(n_steps)]
    coloc_a_id = add_track(coloc_a_pos, [(0.0, 7.500, 0.0)] * n_steps)
    coloc_b_id = add_track(coloc_b_pos, [(0.0, 7.502, 0.0)] * n_steps)

    # 4. Pair that starts close (distance 1.0 km <= 5.0 km and low rel speed 0.001 km/s for the first 3 steps)
    # and then drifts apart to 50 km (> threshold_km, leaving the 27-neighborhood!).
    # Must NOT be falsely classified as co-located; must be classified as a conjunction!
    drift_a_pos = []
    drift_b_pos = []
    for k in range(n_steps):
        drift_a_pos.append((6950.0, 0.0, 0.0))
        if k < 3:
            drift_b_pos.append((6951.0, 0.0, 0.0))
        else:
            drift_b_pos.append((6950.0 + 50.0 * k, 0.0, 0.0))
    drift_a_id = add_track(drift_a_pos, [(0.0, 7.500, 0.0)] * n_steps)
    drift_b_id = add_track(drift_b_pos, [(0.0, 7.501, 0.0)] * n_steps)

    # 5. Add 70 random synthetic objects in 3 altitude shells with random walks and boundary crossings
    for i in range(70):
        band = i % 3
        if band == 0:
            perigee, apogee = 548.0, 562.0
            base_x, base_y, base_z = 6930.0, 0.0, 0.0
        elif band == 1:
            perigee, apogee = 810.0, 825.0
            base_x, base_y, base_z = 7190.0, 0.0, 0.0
        else:
            perigee, apogee = 1200.0, 1220.0
            base_x, base_y, base_z = 7580.0, 0.0, 0.0

        # Place objects within a ~35 km box around base so plenty of random close approaches occur
        x0 = base_x + rng.uniform(-18.0, 18.0)
        y0 = base_y + rng.uniform(-18.0, 18.0)
        z0 = base_z + rng.uniform(-18.0, 18.0)
        vx = rng.uniform(-1.5, 1.5)
        vy = rng.uniform(-1.5, 1.5)
        vz = rng.uniform(-1.5, 1.5)

        pos_list = [(x0 + k * vx, y0 + k * vy, z0 + k * vz) for k in range(n_steps)]
        vel_list = [(vx, vy, vz) for _ in range(n_steps)]
        add_track(pos_list, vel_list, perigee_km=perigee, apogee_km=apogee)

    # Run both the spatial-hash broad-phase screener and the brute-force all-pairs screener
    bp_res = screen_tracks_broadphase(
        tracks_by_norad,
        altitude_bands_by_norad=altitude_bands_by_norad,
        names_by_norad=names_by_norad,
        threshold_km=threshold_km,
        colocated_km=colocated_km,
        colocated_rel_speed_km_s=colocated_rel_speed_km_s,
        grid_km=threshold_km,
    )
    bf_res = screen_tracks_bruteforce(
        tracks_by_norad,
        altitude_bands_by_norad=altitude_bands_by_norad,
        names_by_norad=names_by_norad,
        threshold_km=threshold_km,
        colocated_km=colocated_km,
        colocated_rel_speed_km_s=colocated_rel_speed_km_s,
    )

    # Also cross-check against the legacy _evaluate_pair_horizon function on all pairs
    pad_km = max(threshold_km, colocated_km)
    legacy_conj: dict[tuple[int, int], dict[str, Any]] = {}
    legacy_coloc: dict[tuple[int, int], dict[str, Any]] = {}
    ordered_ids = sorted(tracks_by_norad.keys())
    for i in range(len(ordered_ids)):
        id_a = ordered_ids[i]
        hp_a, ha_a = altitude_bands_by_norad[id_a]
        for j in range(i + 1, len(ordered_ids)):
            id_b = ordered_ids[j]
            hp_b, ha_b = altitude_bands_by_norad[id_b]
            if not altitude_bands_overlap(hp_a, ha_a, hp_b, ha_b, pad_km):
                continue
            ev = _evaluate_pair_horizon(
                tracks_by_norad[id_a],
                tracks_by_norad[id_b],
                frame="TEME",
                threshold_km=threshold_km,
            )
            max_miss = ev.get("max_miss_km")
            max_rel = ev.get("max_relative_speed_km_s")
            is_coloc = (
                max_miss is not None
                and max_rel is not None
                and float(max_miss) <= colocated_km
                and float(max_rel) <= colocated_rel_speed_km_s
            )
            if is_coloc:
                legacy_coloc[(id_a, id_b)] = ev
            elif ev["within_display_threshold"]:
                legacy_conj[(id_a, id_b)] = ev

    bp_conj_map = {(c["primary_norad_id"], c["secondary_norad_id"]): c for c in bp_res.conjunctions}
    bf_conj_map = {(c["primary_norad_id"], c["secondary_norad_id"]): c for c in bf_res.conjunctions}
    bp_coloc_map = {(c["primary_norad_id"], c["secondary_norad_id"]): c for c in bp_res.co_located}
    bf_coloc_map = {(c["primary_norad_id"], c["secondary_norad_id"]): c for c in bf_res.co_located}

    # Verify explicit boundary cases were found
    assert (face_a_id, face_b_id) in bp_conj_map
    assert (corner_a_id, corner_b_id) in bp_conj_map
    assert (coloc_a_id, coloc_b_id) in bp_coloc_map
    assert (drift_a_id, drift_b_id) in bp_conj_map
    assert (drift_a_id, drift_b_id) not in bp_coloc_map

    # Assert identical conjunction pair sets
    assert set(bp_conj_map.keys()) == set(bf_conj_map.keys()) == set(legacy_conj.keys())
    assert len(bp_conj_map) >= 15  # Meaningful number of close approaches tested

    # Assert identical co-located pair sets
    assert set(bp_coloc_map.keys()) == set(bf_coloc_map.keys()) == set(legacy_coloc.keys())

    # Assert tight numerical equivalence for every conjunction
    for pair_key, bp_item in bp_conj_map.items():
        bf_item = bf_conj_map[pair_key]
        leg_item = legacy_conj[pair_key]
        assert bp_item["min_miss_km"] == pytest.approx(bf_item["min_miss_km"], rel=1e-12, abs=1e-12)
        assert bp_item["min_miss_km"] == pytest.approx(leg_item["geometric_miss_km"], rel=1e-9, abs=1e-6)
        assert bp_item["tca"] == bf_item["tca"] == leg_item["time_of_closest_sample"]
        assert bp_item["relative_speed_km_s"] == pytest.approx(
            bf_item["relative_speed_km_s"], rel=1e-12, abs=1e-12
        )

    # Assert tight numerical equivalence for every co-located pair
    for pair_key, bp_item in bp_coloc_map.items():
        bf_item = bf_coloc_map[pair_key]
        leg_item = legacy_coloc[pair_key]
        assert bp_item["min_miss_km"] == pytest.approx(bf_item["min_miss_km"], rel=1e-12, abs=1e-12)
        assert bp_item["min_miss_km"] == pytest.approx(leg_item["geometric_miss_km"], rel=1e-9, abs=1e-6)
        assert bp_item["max_miss_km"] == pytest.approx(bf_item["max_miss_km"], rel=1e-12, abs=1e-12)
        assert bp_item["max_miss_km"] == pytest.approx(leg_item["max_miss_km"], rel=1e-9, abs=1e-6)
        assert bp_item["max_relative_speed_km_s"] == pytest.approx(
            bf_item["max_relative_speed_km_s"], rel=1e-12, abs=1e-12
        )

    # Verify that omitting the 27-neighborhood (same-cell only) MISSES the cross-boundary conjunctions
    broken_no_neighbors = screen_tracks_broadphase(
        tracks_by_norad,
        altitude_bands_by_norad=altitude_bands_by_norad,
        names_by_norad=names_by_norad,
        threshold_km=threshold_km,
        colocated_km=colocated_km,
        colocated_rel_speed_km_s=colocated_rel_speed_km_s,
        grid_km=threshold_km,
        include_27_neighbors=False,
    )
    broken_no_neighbor_pairs = {
        (c["primary_norad_id"], c["secondary_norad_id"]) for c in broken_no_neighbors.conjunctions
    }
    assert (face_a_id, face_b_id) not in broken_no_neighbor_pairs
    assert (corner_a_id, corner_b_id) not in broken_no_neighbor_pairs
    assert broken_no_neighbor_pairs != set(bf_conj_map.keys())

    # Verify that an unclamped cell size smaller than threshold_km (e.g. 2.0 km when threshold=10.0 km)
    # MISSES conjunctions separated by > 2 cells (such as the 8.66 km corner pair)
    broken_small_grid = screen_tracks_broadphase(
        tracks_by_norad,
        altitude_bands_by_norad=altitude_bands_by_norad,
        names_by_norad=names_by_norad,
        threshold_km=threshold_km,
        colocated_km=colocated_km,
        colocated_rel_speed_km_s=colocated_rel_speed_km_s,
        grid_km=2.0,
        clamp_grid=False,
    )
    broken_small_grid_pairs = {
        (c["primary_norad_id"], c["secondary_norad_id"]) for c in broken_small_grid.conjunctions
    }
    assert (corner_a_id, corner_b_id) not in broken_small_grid_pairs
    assert broken_small_grid_pairs != set(bf_conj_map.keys())


def test_tight_cluster_and_far_control_object():
    """
    A compact cluster of objects all within threshold_km (placed across multiple adjacent
    3D cells around a grid corner) and a control object far away in the same altitude band.
    The spatial hash must find every intra-cluster conjunction and must not report any
    conjunction with (or screen) the far object.
    """
    t0 = datetime(2026, 11, 12, 16, 0, 0, tzinfo=timezone.utc)
    threshold_km = 10.0
    cluster_offsets = [
        (-1.5, -1.5, -1.5),
        (-1.5, -1.5, 1.5),
        (-1.5, 1.5, -1.5),
        (-1.5, 1.5, 1.5),
        (1.5, -1.5, -1.5),
        (1.5, -1.5, 1.5),
        (1.5, 1.5, -1.5),
        (1.5, 1.5, 1.5),
    ]
    tracks_by_norad: dict[int, list[dict[str, Any]]] = {}
    altitude_bands_by_norad: dict[int, tuple[float, float]] = {}
    cluster_ids: list[int] = []
    step0_positions: list[tuple[float, float, float]] = []

    # Center the cluster right on the cell corner (7000.0, 0.0, 0.0) so the 8 objects sit in 8 distinct cells!
    for idx, (dx, dy, dz) in enumerate(cluster_offsets):
        nid = 5000 + idx
        cluster_ids.append(nid)
        vx = 0.2 * (idx + 1)
        pos = [(7000.0 + dx, dy, dz), (7000.0 + dx + vx, dy, dz)]
        vel = [(vx, 0.0, 0.0), (vx, 0.0, 0.0)]
        step0_positions.append(pos[0])
        tracks_by_norad[nid] = _make_synthetic_samples(pos, vel, t0=t0)
        altitude_bands_by_norad[nid] = (600.0, 610.0)

    # Control object in the exact same altitude band [600, 610] km, but on the opposite side of Earth
    far_id = 9999
    step0_positions.append((-7000.0, 0.0, 0.0))
    tracks_by_norad[far_id] = _make_synthetic_samples(
        [(-7000.0, 0.0, 0.0), (-7000.0, 5.0, 0.0)],
        [(0.0, 7.5, 0.0), (0.0, 7.5, 0.0)],
        t0=t0,
    )
    altitude_bands_by_norad[far_id] = (600.0, 610.0)

    bins = spatial_hash_bins(np.asarray(step0_positions, dtype=np.float64), cell_size_km=threshold_km)
    # The 8 cluster objects sit in 8 different cells and the far object sits in a 9th cell
    assert len(bins) == 9

    res = screen_tracks_broadphase(
        tracks_by_norad,
        altitude_bands_by_norad=altitude_bands_by_norad,
        threshold_km=threshold_km,
        grid_km=threshold_km,
    )
    m = len(cluster_ids)
    expected_cluster_pairs = m * (m - 1) // 2  # 28 intra-cluster pairs

    # Maximum pairwise distance in the cluster is sqrt(3^2 + 3^2 + 3^2) = sqrt(27) = 5.196 km <= 10 km
    assert res.total_pair_count == (m + 1) * m // 2  # 36 total pairs
    assert res.radial_skipped_pair_count == 0  # All 9 share [600, 610] km altitude band
    assert res.spatial_skipped_pair_count == m  # All 8 pairs involving far_id are spatially skipped
    assert res.screened_pair_count == expected_cluster_pairs
    assert len(res.conjunctions) == expected_cluster_pairs
    assert len(res.co_located) == 0

    found_pairs = {(c["primary_norad_id"], c["secondary_norad_id"]) for c in res.conjunctions}
    for i in range(m):
        for j in range(i + 1, m):
            assert (cluster_ids[i], cluster_ids[j]) in found_pairs
    for c in res.conjunctions:
        assert far_id not in (c["primary_norad_id"], c["secondary_norad_id"])


def test_radial_sieve_skips_non_overlapping_bands_without_exact_screening():
    """
    Verify Layer 1 (apogee/perigee radial-overlap sieve) immediately eliminates pairs
    whose [perigee - pad, apogee + pad] bands do not overlap, without exact screening.
    """
    t0 = datetime(2026, 11, 12, 16, 0, 0, tzinfo=timezone.utc)
    # Create 4 objects with non-overlapping altitude bands (separated by >= 100 km, pad = 10 km).
    # Even if their synthetic 3D coordinates are placed in the same spatial cell, Layer 1
    # must reject them before exact screening.
    altitudes = [(400.0, 410.0), (550.0, 560.0), (750.0, 760.0), (950.0, 960.0)]
    tracks_by_norad: dict[int, list[dict[str, Any]]] = {}
    altitude_bands_by_norad: dict[int, tuple[float, float]] = {}

    for idx, (p_km, a_km) in enumerate(altitudes):
        nid = 3000 + idx
        tracks_by_norad[nid] = _make_synthetic_samples(
            [(6800.0, 0.0, 0.0), (6800.0, 1.0, 0.0)],
            [(0.0, 7.5 + idx * 0.1, 0.0), (0.0, 7.5 + idx * 0.1, 0.0)],
            t0=t0,
        )
        altitude_bands_by_norad[nid] = (p_km, a_km)

    perigees = [b[0] for b in altitudes]
    apogees = [b[1] for b in altitudes]
    assert count_radial_skipped_pairs(perigees, apogees, pad_km=10.0) == 6

    res = screen_tracks_broadphase(
        tracks_by_norad,
        altitude_bands_by_norad=altitude_bands_by_norad,
        threshold_km=10.0,
    )
    assert res.total_pair_count == 6
    assert res.radial_skipped_pair_count == 6
    assert res.spatial_skipped_pair_count == 0
    assert res.skipped_pair_count == 6
    assert res.screened_pair_count == 0
    assert res.conjunctions == []
    assert res.co_located == []


def test_count_identities_and_grid_km_clamping(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """
    Verify:
    - screened + radial_skipped + spatial_skipped == n*(n-1)//2
    - skipped_pair_count == radial_skipped + spatial_skipped
    - ORBITGUARD_LOOP_GRID_KM clamps values < threshold_km up to threshold_km, and honors values >= threshold_km
    - /api/orbitguard/loop/status and /api/orbitguard/loop/conjunctions expose all broad-phase counts
    """
    # 1. Unit-check resolve_grid_km clamping behavior
    assert resolve_grid_km(None, 10.0) == pytest.approx(10.0)
    assert resolve_grid_km(2.5, 10.0) == pytest.approx(10.0)  # Clamped up to threshold
    assert resolve_grid_km(0.0, 10.0) == pytest.approx(10.0)  # Clamped up to threshold
    assert resolve_grid_km(-5.0, 10.0) == pytest.approx(10.0)  # Clamped up to threshold
    assert resolve_grid_km(25.0, 10.0) == pytest.approx(25.0)  # >= threshold preserved

    # 2. Check env var clamping on TrackingLoop
    monkeypatch.setenv("ORBITGUARD_LOOP_LIVE_FETCH", "0")
    monkeypatch.setenv("ORBITGUARD_LOOP_GRID_KM", "3.0")
    loop_clamped = TrackingLoop(tmp_path / "clamped", threshold_km=10.0, step_s=30.0)
    assert loop_clamped.grid_km == pytest.approx(10.0)

    monkeypatch.setenv("ORBITGUARD_LOOP_GRID_KM", "25.0")
    loop_custom = TrackingLoop(tmp_path / "custom", threshold_km=10.0, step_s=30.0)
    assert loop_custom.grid_km == pytest.approx(25.0)

    # 3. Verify count identities through TrackingLoop and HTTP status/conjunctions endpoints
    service = MonitorService(
        tmp_path / "api",
        transport=lambda url: HttpResult(504, b"offline", url),
    )
    loop = TrackingLoop(tmp_path / "api", store=service.store, threshold_km=10.0, grid_km=4.0, step_s=30.0)
    assert loop.grid_km == pytest.approx(10.0)
    service.loop = loop
    app = FastAPI()
    app.include_router(build_test_router(service, loop=loop))
    client = TestClient(app)

    run_body = client.post("/api/orbitguard/loop/run-once").json()
    n = run_body["object_count"]
    expected_total = n * (n - 1) // 2

    for payload in (
        run_body,
        client.get("/api/orbitguard/loop/status").json(),
        client.get("/api/orbitguard/loop/conjunctions").json(),
    ):
        assert payload["total_pair_count"] == expected_total
        assert (
            payload["screened_pair_count"]
            + payload["radial_skipped_pair_count"]
            + payload["spatial_skipped_pair_count"]
            == expected_total
        )
        assert (
            payload["radial_skipped_pair_count"] + payload["spatial_skipped_pair_count"]
            == payload["skipped_pair_count"]
        )
        assert payload["screened_pair_count"] + payload["skipped_pair_count"] == expected_total
        assert payload["grid_km"] == pytest.approx(10.0)
        assert payload["radial_skipped_pair_count"] > 0
        assert payload["spatial_skipped_pair_count"] > 0


def test_cap_policy_prioritizes_stations_and_debris_over_active(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """
    Verify that when ORBITGUARD_LOOP_MAX_OBJECTS caps the catalog, stations and named
    debris groups (cosmos-1408-debris, cosmos-2251-debris, fengyun-1c-debris,
    iridium-33-debris) are kept first and never starved by `active`, even though
    `active` appears second in DEFAULT_LOOP_GROUPS.
    """
    assert DEFAULT_LOOP_MAX_OBJECTS == 3000
    assert set(PRIORITY_DEBRIS_GROUPS) == {
        "cosmos-1408-debris",
        "cosmos-2251-debris",
        "fengyun-1c-debris",
        "iridium-33-debris",
    }

    def make_item(group: str, norad_id: int) -> dict[str, Any]:
        return {
            "source_group": group,
            "source_groups": [group],
            "record": {"NORAD_CAT_ID": norad_id},
        }

    # Unit check cap_sort_key ordering: stations < named debris < other < active
    assert cap_sort_key(make_item("stations", 25544), DEFAULT_LOOP_GROUPS) < cap_sort_key(
        make_item("fengyun-1c-debris", 1), DEFAULT_LOOP_GROUPS
    )
    assert cap_sort_key(make_item("fengyun-1c-debris", 99999), DEFAULT_LOOP_GROUPS) < cap_sort_key(
        make_item("active", 1), DEFAULT_LOOP_GROUPS
    )
    assert cap_sort_key(make_item("cosmos-2251-debris", 99999), DEFAULT_LOOP_GROUPS) < cap_sort_key(
        make_item("active", 1), DEFAULT_LOOP_GROUPS
    )
    assert cap_sort_key(make_item("iridium-33-debris", 99999), DEFAULT_LOOP_GROUPS) < cap_sort_key(
        make_item("active", 1), DEFAULT_LOOP_GROUPS
    )
    assert cap_sort_key(make_item("cosmos-1408-debris", 99999), DEFAULT_LOOP_GROUPS) < cap_sort_key(
        make_item("active", 1), DEFAULT_LOOP_GROUPS
    )

    base_rec = json.loads(Path("data/gp/stations.json").read_text(encoding="utf-8"))[0]

    def make_group_records(group_prefix: str, start_norad: int, count: int) -> list[dict[str, Any]]:
        recs = []
        for idx in range(count):
            r = dict(base_rec)
            r["OBJECT_NAME"] = f"{group_prefix}-{idx}"
            r["NORAD_CAT_ID"] = start_norad + idx
            recs.append(r)
        return recs

    # Simulate DEFAULT_LOOP_GROUPS where `active` has 50 objects (with low NORAD IDs!)
    # and debris groups come AFTER `active` in group order.
    group_payloads = {
        "stations": make_group_records("STATION", 1000, 3),
        "active": make_group_records("ACTIVE", 100, 50),
        "cosmos-1408-debris": make_group_records("C1408", 2000, 2),
        "cosmos-2251-debris": make_group_records("C2251", 3000, 4),
        "fengyun-1c-debris": make_group_records("FY1C", 4000, 5),
        "iridium-33-debris": make_group_records("IR33", 5000, 3),
    }

    # Total priority objects = 3 (stations) + 2 + 4 + 5 + 3 (debris) = 17.
    # With max_objects = 20, all 17 priority objects must be kept, plus 3 from `active` (lowest NORAD IDs: 100, 101, 102).
    def mock_transport(url: str) -> HttpResult:
        for grp, recs in group_payloads.items():
            if f"GROUP={grp}" in url:
                return HttpResult(200, json.dumps(recs).encode("utf-8"), url)
        return HttpResult(404, b"not found", url)

    now = datetime(2026, 11, 12, 16, 0, 0, tzinfo=timezone.utc)
    loop = TrackingLoop(
        tmp_path / "cap_test",
        transport=mock_transport,
        live_fetch=True,
        max_objects=20,
        step_s=60.0,
    )
    report = loop.run_once(now=now)
    assert report["status"] == "ok"
    assert report["object_count"] == 20
    assert report["objects_capped"] is True
    assert report["objects_before_cap"] == 3 + 50 + 2 + 4 + 5 + 3
    assert report["objects_after_cap"] == 20
    assert report["cap"]["objects_dropped_by_cap"] == (3 + 50 + 2 + 4 + 5 + 3) - 20

    objs = loop.objects(now=now)["objects"]
    by_group: dict[str, list[int]] = {}
    for o in objs:
        by_group.setdefault(o["group"], []).append(o["norad_id"])

    assert len(by_group.get("stations", [])) == 3
    assert len(by_group.get("cosmos-1408-debris", [])) == 2
    assert len(by_group.get("cosmos-2251-debris", [])) == 4
    assert len(by_group.get("fengyun-1c-debris", [])) == 5
    assert len(by_group.get("iridium-33-debris", [])) == 3
    assert sorted(by_group.get("active", [])) == [100, 101, 102]


def test_committed_debris_catalog_broadphase_timing_and_accounting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """
    Timing and accounting verification on the real committed LEO debris snapshots
    (stations + cosmos-1408-debris + cosmos-2251-debris + fengyun-1c-debris + iridium-33-debris,
    N=2698 LEO objects, 3,638,253 total pairs). Does not assert a hard wall-clock limit
    to avoid CI flakiness, but records timings and verifies exact count identities.
    """
    gp_dir = Path("data/gp")

    def local_gp_transport(url: str, headers: dict[str, str] | None = None) -> HttpResult:
        fname = url.rsplit("/", 1)[-1].split("?")[0]
        local_file = gp_dir / fname
        if local_file.is_file():
            return HttpResult(200, local_file.read_bytes(), url)
        return HttpResult(404, b"not found", url)

    monkeypatch.setenv(
        "ORBITGUARD_LOOP_GROUPS",
        "stations,cosmos-1408-debris,cosmos-2251-debris,fengyun-1c-debris,iridium-33-debris",
    )
    now = datetime(2026, 11, 12, 20, 15, 0, tzinfo=timezone.utc)
    loop = TrackingLoop(
        tmp_path / "debris_bench",
        transport=local_gp_transport,
        github_public=True,
        max_objects=3000,
        step_s=30.0,
        threshold_km=10.0,
    )

    t0 = time.perf_counter()
    report = loop.run_once(now=now)
    elapsed_s = time.perf_counter() - t0

    n = report["object_count"]
    expected_total_pairs = n * (n - 1) // 2
    assert n >= 2600
    assert report["total_pair_count"] == expected_total_pairs
    assert (
        report["screened_pair_count"]
        + report["radial_skipped_pair_count"]
        + report["spatial_skipped_pair_count"]
        == expected_total_pairs
    )
    assert (
        report["radial_skipped_pair_count"] + report["spatial_skipped_pair_count"]
        == report["skipped_pair_count"]
    )
    assert report["radial_skipped_pair_count"] > 1_000_000
    assert report["spatial_skipped_pair_count"] > 2_000_000
    assert report["screened_pair_count"] < 5_000
    assert report["label"] == SCREENING_LABEL
    print(
        f"\n[broadphase benchmark] N={n} total_pairs={expected_total_pairs} "
        f"radial_skipped={report['radial_skipped_pair_count']} "
        f"spatial_skipped={report['spatial_skipped_pair_count']} "
        f"screened={report['screened_pair_count']} "
        f"conjunctions={report['conjunction_count']} "
        f"co_located={report['co_located_count']} "
        f"run_once_elapsed_s={elapsed_s:.3f}"
    )
