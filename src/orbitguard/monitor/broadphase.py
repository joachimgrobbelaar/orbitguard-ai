"""Two-layer broad-phase conjunction pre-filter for real-time LEO catalog screening.

Layer 1 — Apogee/perigee (radial-overlap) sieve:
    Eliminates pairs whose ``[perigee - pad, apogee + pad]`` altitude bands do
    not overlap, where ``pad = max(threshold_km, colocated_km)``. Computed in
    ``O(n log n)`` via sorted interval bounds.

Layer 2 — 3D spatial hashing per time step with 27-neighborhood lookup:
    At each shared propagation time step, bins every object's TEME/ECI position
    into a uniform 3D grid with cell size ``grid_km >= max(threshold_km, colocated_km)``
    (``cell_index = floor(position / cell_size)`` along each axis). Only pairs
    falling in the same cell or one of the 26 adjacent cells (the full 3x3x3
    27-neighborhood across faces, edges, and corners) that also pass the radial
    sieve receive an exact Euclidean miss-distance and relative-speed evaluation.

Co-located (docked / attached) classification invariant:
    A pair is classified as ``co_located`` if and only if it was present in the
    27-neighborhood at EVERY shared sample across the horizon AND its measured
    ``max_miss_km <= colocated_km`` and ``max_relative_speed_km_s <= colocated_rel_speed_km_s``.
    If a pair is absent from the 27-neighborhood at any shared sample, its
    separation at that step exceeds ``cell_size >= colocated_km``, so
    ``max_miss_km > colocated_km`` and it cannot be co-located.

Scientific honesty:
    Geometric miss-distance screening only, flagged for human review. No
    collision probability (Pc) or expected collision count (Nc) is computed.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Sequence

import numpy as np

from orbitguard.nd import GP_REFERENCE_FRAME

NEIGHBOR_OFFSETS_27: tuple[tuple[int, int, int], ...] = tuple(
    (dx, dy, dz)
    for dx in (-1, 0, 1)
    for dy in (-1, 0, 1)
    for dz in (-1, 0, 1)
)

POSITIVE_NEIGHBOR_OFFSETS_13: tuple[tuple[int, int, int], ...] = tuple(
    (dx, dy, dz)
    for dx, dy, dz in NEIGHBOR_OFFSETS_27
    if (dx, dy, dz) > (0, 0, 0)
)

_PACK_OFFSET = 1 << 20
_PACK_MAX_ABS_COORD = _PACK_OFFSET - 4
_SHIFT_X = 42
_SHIFT_Y = 21
_DELTA_KEYS_13 = np.array(
    [(dx << _SHIFT_X) + (dy << _SHIFT_Y) + dz for dx, dy, dz in POSITIVE_NEIGHBOR_OFFSETS_13],
    dtype=np.int64,
)


def resolve_grid_km(grid_km: Optional[float], threshold_km: float) -> float:
    """Return effective 3D spatial-hash cell size in km, clamped to ``>= threshold_km``."""
    floor_km = max(0.0, float(threshold_km))
    if grid_km is None:
        return floor_km
    try:
        val = float(grid_km)
    except (TypeError, ValueError):
        return floor_km
    if not np.isfinite(val):
        return floor_km
    return max(floor_km, val)


def altitude_bands_overlap(
    perigee_a_km: float,
    apogee_a_km: float,
    perigee_b_km: float,
    apogee_b_km: float,
    threshold_km: float,
) -> bool:
    """Return True when two ``[perigee, apogee]`` altitude bands padded by ``threshold_km`` overlap."""
    pad = max(0.0, float(threshold_km))
    return (perigee_a_km - pad) <= (apogee_b_km + pad) and (perigee_b_km - pad) <= (apogee_a_km + pad)


def count_radial_skipped_pairs(
    perigees_km: Sequence[float] | np.ndarray,
    apogees_km: Sequence[float] | np.ndarray,
    pad_km: float,
) -> int:
    """Count unordered pairs whose ``[perigee - pad, apogee + pad]`` bands do not overlap in ``O(n log n)``."""
    hp = np.asarray(perigees_km, dtype=np.float64)
    ha = np.asarray(apogees_km, dtype=np.float64)
    n = len(hp)
    if n < 2:
        return 0
    pad = max(0.0, float(pad_km))
    low = np.minimum(hp, ha) - pad
    high = np.maximum(hp, ha) + pad
    sorted_low = np.sort(low)
    non_overlapping = n - np.searchsorted(sorted_low, high, side="right")
    return int(np.sum(non_overlapping))


def spatial_hash_bins(
    positions_km: np.ndarray,
    cell_size_km: float,
) -> dict[tuple[int, int, int], list[int]]:
    """Bin 3D positions ``(m, 3)`` into integer cell keys ``floor(pos / cell_size_km)``."""
    pos = np.asarray(positions_km, dtype=np.float64)
    cell = max(1e-6, float(cell_size_km))
    coords = np.floor(pos / cell).astype(np.int64)
    bins: dict[tuple[int, int, int], list[int]] = defaultdict(list)
    for idx, (cx, cy, cz) in enumerate(coords.tolist()):
        bins[(int(cx), int(cy), int(cz))].append(idx)
    return dict(bins)


@dataclass
class BroadphaseScreenResult:
    """Structured result of two-layer broad-phase and exact conjunction screening."""

    total_pair_count: int
    screened_pair_count: int
    skipped_pair_count: int
    radial_skipped_pair_count: int
    spatial_skipped_pair_count: int
    grid_km: float
    threshold_km: float
    colocated_km: float
    colocated_rel_speed_km_s: float
    conjunctions: list[dict[str, Any]] = field(default_factory=list)
    co_located: list[dict[str, Any]] = field(default_factory=list)
    evaluated_pairs: dict[tuple[int, int], dict[str, Any]] = field(default_factory=dict)

    @property
    def conjunction_count(self) -> int:
        return len(self.conjunctions)

    @property
    def co_located_count(self) -> int:
        return len(self.co_located)

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_pair_count": self.total_pair_count,
            "screened_pair_count": self.screened_pair_count,
            "skipped_pair_count": self.skipped_pair_count,
            "radial_skipped_pair_count": self.radial_skipped_pair_count,
            "spatial_skipped_pair_count": self.spatial_skipped_pair_count,
            "grid_km": self.grid_km,
            "threshold_km": self.threshold_km,
            "colocated_km": self.colocated_km,
            "colocated_rel_speed_km_s": self.colocated_rel_speed_km_s,
            "conjunction_count": len(self.conjunctions),
            "co_located_count": len(self.co_located),
            "conjunctions": self.conjunctions,
            "co_located": self.co_located,
        }


def _extract_grid_tensors(
    sorted_ids: Sequence[int],
    tracks_by_norad: Mapping[int, Sequence[Mapping[str, Any]]],
) -> tuple[list[str], np.ndarray, np.ndarray, np.ndarray]:
    """Build dense ``(T, n, 3)`` position/velocity tensors and ``(T, n)`` presence mask."""
    n = len(sorted_ids)
    time_to_idx: dict[str, int] = {}
    time_strs: list[str] = []

    for nid in sorted_ids:
        for row in tracks_by_norad.get(nid, ()):
            t_str = row.get("time")
            if not t_str:
                continue
            r_val = row.get("r_teme_km") if "r_teme_km" in row else row.get("r_km")
            v_val = row.get("v_teme_km_s") if "v_teme_km_s" in row else row.get("v_km_s")
            if r_val is None or v_val is None:
                continue
            if t_str not in time_to_idx:
                time_to_idx[t_str] = len(time_strs)
                time_strs.append(str(t_str))

    # Ensure chronological ordering if tracks had non-aligned start times
    if len(time_strs) > 1 and any(time_strs[i] > time_strs[i + 1] for i in range(len(time_strs) - 1)):
        time_strs.sort()
        time_to_idx = {t_str: idx for idx, t_str in enumerate(time_strs)}

    num_steps = len(time_strs)
    pos = np.zeros((num_steps, n, 3), dtype=np.float64)
    vel = np.zeros((num_steps, n, 3), dtype=np.float64)
    present = np.zeros((num_steps, n), dtype=bool)

    if num_steps == 0 or n == 0:
        return time_strs, pos, vel, present

    for i, nid in enumerate(sorted_ids):
        track = tracks_by_norad.get(nid, ())
        if not track:
            continue
        first = track[0]
        if "r_teme_km" in first and "v_teme_km_s" in first:
            t_indices = [time_to_idx[row["time"]] for row in track if "time" in row and "r_teme_km" in row]
            if len(t_indices) == len(track):
                pos[t_indices, i] = [row["r_teme_km"] for row in track]
                vel[t_indices, i] = [row["v_teme_km_s"] for row in track]
                present[t_indices, i] = True
                continue
        for row in track:
            t_str = row.get("time")
            if not t_str or t_str not in time_to_idx:
                continue
            r_val = row.get("r_teme_km") if "r_teme_km" in row else row.get("r_km")
            v_val = row.get("v_teme_km_s") if "v_teme_km_s" in row else row.get("v_km_s")
            if r_val is None or v_val is None:
                continue
            t_idx = time_to_idx[t_str]
            if not present[t_idx, i]:
                pos[t_idx, i] = r_val
                vel[t_idx, i] = v_val
                present[t_idx, i] = True

    return time_strs, pos, vel, present


def _find_step_candidate_pairs_tuple_fallback(
    coords: np.ndarray,
    obj_idx: np.ndarray,
    *,
    include_27_neighbors: bool,
) -> tuple[np.ndarray, np.ndarray]:
    """Fallback 3D tuple-dict spatial hash when cell coordinates exceed 21-bit packing bounds."""
    bins: dict[tuple[int, int, int], list[int]] = defaultdict(list)
    for local_i, (cx, cy, cz) in enumerate(coords.tolist()):
        bins[(int(cx), int(cy), int(cz))].append(int(obj_idx[local_i]))

    step_a: list[int] = []
    step_b: list[int] = []
    for cell_key, members in bins.items():
        m_len = len(members)
        if m_len > 1:
            for p in range(m_len):
                a = members[p]
                for q in range(p + 1, m_len):
                    b = members[q]
                    if a < b:
                        step_a.append(a)
                        step_b.append(b)
                    else:
                        step_a.append(b)
                        step_b.append(a)
        if include_27_neighbors:
            cx, cy, cz = cell_key
            for dx, dy, dz in POSITIVE_NEIGHBOR_OFFSETS_13:
                nbr = bins.get((cx + dx, cy + dy, cz + dz))
                if not nbr:
                    continue
                for a in members:
                    for b in nbr:
                        if a < b:
                            step_a.append(a)
                            step_b.append(b)
                        else:
                            step_a.append(b)
                            step_b.append(a)

    if not step_a:
        return np.empty(0, dtype=np.int64), np.empty(0, dtype=np.int64)
    return np.asarray(step_a, dtype=np.int64), np.asarray(step_b, dtype=np.int64)


def _find_step_candidate_pairs(
    pos_t: np.ndarray,
    obj_idx: np.ndarray,
    cell_size: float,
    *,
    include_27_neighbors: bool = True,
) -> tuple[np.ndarray, np.ndarray]:
    """Return deduplicated ``(idx_a, idx_b)`` with ``idx_a < idx_b`` in the 27-neighborhood at step ``t``."""
    raw_coords = np.floor(pos_t / cell_size).astype(np.int64)
    if int(np.max(np.abs(raw_coords))) >= _PACK_MAX_ABS_COORD:
        return _find_step_candidate_pairs_tuple_fallback(
            raw_coords,
            obj_idx,
            include_27_neighbors=include_27_neighbors,
        )

    coords = raw_coords + _PACK_OFFSET
    cell_keys = (coords[:, 0] << _SHIFT_X) | (coords[:, 1] << _SHIFT_Y) | coords[:, 2]
    order = np.argsort(cell_keys, kind="mergesort")
    sorted_keys = cell_keys[order]
    sorted_objs = obj_idx[order]

    unique_keys, cell_start, cell_counts = np.unique(
        sorted_keys, return_index=True, return_counts=True
    )
    num_cells = len(unique_keys)
    step_a: list[np.ndarray] = []
    step_b: list[np.ndarray] = []

    # 1. Same-cell pairs (offset (0, 0, 0))
    multi = np.flatnonzero(cell_counts > 1)
    for u in multi:
        s = int(cell_start[u])
        c = int(cell_counts[u])
        objs_u = sorted_objs[s : s + c]
        ii, jj = np.triu_indices(c, k=1)
        step_a.append(objs_u[ii])
        step_b.append(objs_u[jj])

    # 2. Adjacent-cell pairs across all 26 neighbor cells (via 13 positive offsets + symmetry)
    if include_27_neighbors and num_cells > 1:
        target_keys = unique_keys[:, None] + _DELTA_KEYS_13[None, :]
        idx = np.searchsorted(unique_keys, target_keys)
        idx_clamped = np.minimum(idx, num_cells - 1)
        hit_u, hit_d = np.nonzero((idx < num_cells) & (unique_keys[idx_clamped] == target_keys))
        if len(hit_u) > 0:
            hit_v = idx[hit_u, hit_d]
            single_mask = (cell_counts[hit_u] == 1) & (cell_counts[hit_v] == 1)
            if np.any(single_mask):
                su = hit_u[single_mask]
                sv = hit_v[single_mask]
                oa = sorted_objs[cell_start[su]]
                ob = sorted_objs[cell_start[sv]]
                step_a.append(np.minimum(oa, ob))
                step_b.append(np.maximum(oa, ob))
            if not np.all(single_mask):
                for u, v in zip(hit_u[~single_mask], hit_v[~single_mask]):
                    su = int(cell_start[u])
                    cu = int(cell_counts[u])
                    sv = int(cell_start[v])
                    cv = int(cell_counts[v])
                    objs_u = sorted_objs[su : su + cu]
                    objs_v = sorted_objs[sv : sv + cv]
                    aa = np.repeat(objs_u, cv)
                    bb = np.tile(objs_v, cu)
                    step_a.append(np.minimum(aa, bb))
                    step_b.append(np.maximum(aa, bb))

    if not step_a:
        return np.empty(0, dtype=np.int64), np.empty(0, dtype=np.int64)
    return np.concatenate(step_a), np.concatenate(step_b)


def screen_tracks_broadphase(
    tracks_by_norad: Mapping[int, Sequence[Mapping[str, Any]]],
    *,
    altitude_bands_by_norad: Optional[Mapping[int, tuple[float, float]]] = None,
    names_by_norad: Optional[Mapping[int, str]] = None,
    threshold_km: float = 10.0,
    colocated_km: float = 5.0,
    colocated_rel_speed_km_s: float = 0.01,
    grid_km: Optional[float] = None,
    frame: str = GP_REFERENCE_FRAME,
    screening_label: str = (
        "geometric screening - miss distance from propagated GP states, "
        "no collision probability, for human review"
    ),
    colocated_reason: str = (
        "near-identical propagated states over the whole horizon; "
        "likely docked/attached or sharing an element set - not a close approach"
    ),
    clamp_grid: bool = True,
    include_27_neighbors: bool = True,
    populate_evaluated_pairs: bool = False,
) -> BroadphaseScreenResult:
    """Screen tracks using the two-layer broad-phase (radial sieve + 3D spatial hash)."""
    thresh = max(0.0, float(threshold_km))
    coloc_dist = max(0.0, float(colocated_km))
    coloc_speed = max(0.0, float(colocated_rel_speed_km_s))
    pad_km = max(thresh, coloc_dist)

    if clamp_grid:
        effective_grid_km = resolve_grid_km(grid_km, thresh)
        cell_size = max(effective_grid_km, coloc_dist, 1e-6)
    else:
        raw_grid = float(grid_km) if grid_km is not None else thresh
        effective_grid_km = max(1e-6, raw_grid)
        cell_size = effective_grid_km

    sorted_ids = sorted(int(nid) for nid in tracks_by_norad.keys())
    n = len(sorted_ids)
    total_pairs = n * (n - 1) // 2

    if n < 2:
        return BroadphaseScreenResult(
            total_pair_count=0,
            screened_pair_count=0,
            skipped_pair_count=0,
            radial_skipped_pair_count=0,
            spatial_skipped_pair_count=0,
            grid_km=effective_grid_km,
            threshold_km=thresh,
            colocated_km=coloc_dist,
            colocated_rel_speed_km_s=coloc_speed,
        )

    # Layer 1: Apogee/perigee radial-overlap sieve
    if altitude_bands_by_norad is not None:
        hp_arr = np.array(
            [float(altitude_bands_by_norad[nid][0]) for nid in sorted_ids],
            dtype=np.float64,
        )
        ha_arr = np.array(
            [float(altitude_bands_by_norad[nid][1]) for nid in sorted_ids],
            dtype=np.float64,
        )
        low_arr = np.minimum(hp_arr, ha_arr) - pad_km
        high_arr = np.maximum(hp_arr, ha_arr) + pad_km
        radial_skipped = count_radial_skipped_pairs(hp_arr, ha_arr, pad_km)
        has_radial_bands = True
    else:
        low_arr = np.zeros(n, dtype=np.float64)
        high_arr = np.zeros(n, dtype=np.float64)
        radial_skipped = 0
        has_radial_bands = False

    # Layer 2: 3D spatial hashing per time step across the 27-neighborhood
    time_strs, pos, vel, present = _extract_grid_tensors(sorted_ids, tracks_by_norad)
    num_steps = len(time_strs)

    pair_id_chunks: list[np.ndarray] = []
    t_idx_chunks: list[np.ndarray] = []
    dist_chunks: list[np.ndarray] = []
    speed_chunks: list[np.ndarray] = []

    for t in range(num_steps):
        obj_idx = np.flatnonzero(present[t])
        if len(obj_idx) < 2:
            continue
        ia, ib = _find_step_candidate_pairs(
            pos[t, obj_idx],
            obj_idx,
            cell_size,
            include_27_neighbors=include_27_neighbors,
        )
        if len(ia) == 0:
            continue
        if has_radial_bands:
            radial_ok = (low_arr[ia] <= high_arr[ib]) & (low_arr[ib] <= high_arr[ia])
            ia = ia[radial_ok]
            ib = ib[radial_ok]
            if len(ia) == 0:
                continue

        dr = pos[t, ia] - pos[t, ib]
        dv = vel[t, ia] - vel[t, ib]
        d_km = np.linalg.norm(dr, axis=1)
        v_kms = np.linalg.norm(dv, axis=1)

        pair_id_chunks.append(ia * n + ib)
        t_idx_chunks.append(np.full(len(ia), t, dtype=np.int32))
        dist_chunks.append(d_km)
        speed_chunks.append(v_kms)

    if not pair_id_chunks:
        spatial_skipped = max(0, total_pairs - radial_skipped)
        return BroadphaseScreenResult(
            total_pair_count=total_pairs,
            screened_pair_count=0,
            skipped_pair_count=radial_skipped + spatial_skipped,
            radial_skipped_pair_count=radial_skipped,
            spatial_skipped_pair_count=spatial_skipped,
            grid_km=effective_grid_km,
            threshold_km=thresh,
            colocated_km=coloc_dist,
            colocated_rel_speed_km_s=coloc_speed,
        )

    all_pairs = np.concatenate(pair_id_chunks)
    all_t = np.concatenate(t_idx_chunks)
    all_dist = np.concatenate(dist_chunks)
    all_speed = np.concatenate(speed_chunks)

    # Stable sort by pair_id preserves chronological time-step order within each pair
    order = np.argsort(all_pairs, kind="mergesort")
    sp = all_pairs[order]
    st = all_t[order]
    sd = all_dist[order]
    ss = all_speed[order]

    unique_p, g_start, g_count = np.unique(sp, return_index=True, return_counts=True)
    screened_pairs = int(len(unique_p))
    spatial_skipped = max(0, total_pairs - radial_skipped - screened_pairs)
    skipped_pairs = radial_skipped + spatial_skipped

    min_miss_arr = np.minimum.reduceat(sd, g_start)
    max_miss_arr = np.maximum.reduceat(sd, g_start)
    max_speed_arr = np.maximum.reduceat(ss, g_start)

    ia_unique = unique_p // n
    ib_unique = unique_p % n
    shared_steps_arr = np.count_nonzero(present[:, ia_unique] & present[:, ib_unique], axis=0)

    colocated_mask = (
        (coloc_dist > 0.0)
        & (coloc_speed > 0.0)
        & (g_count == shared_steps_arr)
        & (shared_steps_arr > 0)
        & (max_miss_arr <= coloc_dist)
        & (max_speed_arr <= coloc_speed)
    )
    conjunction_mask = (~colocated_mask) & (min_miss_arr <= thresh)

    conjunctions: list[dict[str, Any]] = []
    co_located: list[dict[str, Any]] = []
    evaluated_pairs: dict[tuple[int, int], dict[str, Any]] = {}

    if populate_evaluated_pairs:
        indices_to_inspect = np.arange(screened_pairs, dtype=np.int64)
    else:
        indices_to_inspect = np.flatnonzero(colocated_mask | conjunction_mask)

    for k in indices_to_inspect.tolist():
        s_idx = int(g_start[k])
        cnt = int(g_count[k])
        local_argmin = 0 if cnt == 1 else int(np.argmin(sd[s_idx : s_idx + cnt]))
        best_pos = s_idx + local_argmin

        idx_a = int(ia_unique[k])
        idx_b = int(ib_unique[k])
        id_a = sorted_ids[idx_a]
        id_b = sorted_ids[idx_b]
        name_a = str(names_by_norad[id_a]) if (names_by_norad and id_a in names_by_norad) else str(id_a)
        name_b = str(names_by_norad[id_b]) if (names_by_norad and id_b in names_by_norad) else str(id_b)
        pair_key = f"{id_a}-{id_b}"

        tca_str = time_strs[int(st[best_pos])]
        min_miss = float(min_miss_arr[k])
        max_miss = float(max_miss_arr[k])
        rel_speed_tca = float(ss[best_pos])
        max_rel_speed = float(max_speed_arr[k])
        seen_cnt = int(g_count[k])
        shared_cnt = int(shared_steps_arr[k])
        is_coloc = bool(colocated_mask[k])
        is_conj = bool(conjunction_mask[k])

        if is_coloc:
            classification = "co_located"
            co_located.append(
                {
                    "pair": pair_key,
                    "classification": "co_located",
                    "primary_norad_id": id_a,
                    "primary_name": name_a,
                    "secondary_norad_id": id_b,
                    "secondary_name": name_b,
                    "tca": tca_str,
                    "min_miss_km": min_miss,
                    "geometric_miss_km": min_miss,
                    "max_miss_km": max_miss,
                    "max_separation_km": max_miss,
                    "relative_speed_km_s": rel_speed_tca,
                    "max_relative_speed_km_s": max_rel_speed,
                    "colocated_km": coloc_dist,
                    "colocated_rel_speed_km_s": coloc_speed,
                    "frame": frame,
                    "reason": colocated_reason,
                    "label": screening_label,
                    "neighbor_steps": seen_cnt,
                    "shared_steps": shared_cnt,
                }
            )
        elif is_conj:
            classification = "conjunction"
            conjunctions.append(
                {
                    "pair": pair_key,
                    "classification": "conjunction",
                    "primary_norad_id": id_a,
                    "primary_name": name_a,
                    "secondary_norad_id": id_b,
                    "secondary_name": name_b,
                    "tca": tca_str,
                    "min_miss_km": min_miss,
                    "geometric_miss_km": min_miss,
                    "relative_speed_km_s": rel_speed_tca,
                    "threshold_km": thresh,
                    "frame": frame,
                    "label": screening_label,
                    "screening_kind": "geometric_screening",
                    "human_review_required": True,
                    "neighbor_steps": seen_cnt,
                    "shared_steps": shared_cnt,
                }
            )
        else:
            classification = "clear"

        if populate_evaluated_pairs:
            evaluated_pairs[(id_a, id_b)] = {
                "pair": pair_key,
                "primary_norad_id": id_a,
                "secondary_norad_id": id_b,
                "tca": tca_str,
                "min_miss_km": min_miss,
                "max_miss_km": max_miss,
                "relative_speed_km_s": rel_speed_tca,
                "max_relative_speed_km_s": max_rel_speed,
                "neighbor_steps": seen_cnt,
                "shared_steps": shared_cnt,
                "classification": classification,
            }

    return BroadphaseScreenResult(
        total_pair_count=total_pairs,
        screened_pair_count=screened_pairs,
        skipped_pair_count=skipped_pairs,
        radial_skipped_pair_count=radial_skipped,
        spatial_skipped_pair_count=spatial_skipped,
        grid_km=effective_grid_km,
        threshold_km=thresh,
        colocated_km=coloc_dist,
        colocated_rel_speed_km_s=coloc_speed,
        conjunctions=conjunctions,
        co_located=co_located,
        evaluated_pairs=evaluated_pairs,
    )


def screen_tracks_bruteforce(
    tracks_by_norad: Mapping[int, Sequence[Mapping[str, Any]]],
    *,
    altitude_bands_by_norad: Optional[Mapping[int, tuple[float, float]]] = None,
    names_by_norad: Optional[Mapping[int, str]] = None,
    threshold_km: float = 10.0,
    colocated_km: float = 5.0,
    colocated_rel_speed_km_s: float = 0.01,
    frame: str = GP_REFERENCE_FRAME,
    screening_label: str = (
        "geometric screening - miss distance from propagated GP states, "
        "no collision probability, for human review"
    ),
    colocated_reason: str = (
        "near-identical propagated states over the whole horizon; "
        "likely docked/attached or sharing an element set - not a close approach"
    ),
    populate_evaluated_pairs: bool = False,
) -> BroadphaseScreenResult:
    """Reference all-pairs brute-force screener for verification and equivalence testing."""
    thresh = max(0.0, float(threshold_km))
    coloc_dist = max(0.0, float(colocated_km))
    coloc_speed = max(0.0, float(colocated_rel_speed_km_s))
    pad_km = max(thresh, coloc_dist)

    sorted_ids = sorted(int(nid) for nid in tracks_by_norad.keys())
    n = len(sorted_ids)
    total_pairs = n * (n - 1) // 2

    conjunctions: list[dict[str, Any]] = []
    co_located: list[dict[str, Any]] = []
    evaluated_pairs: dict[tuple[int, int], dict[str, Any]] = {}
    radial_skipped = 0
    screened_pairs = 0

    for idx_a in range(n):
        id_a = sorted_ids[idx_a]
        track_a = tracks_by_norad[id_a]
        hp_a, ha_a = altitude_bands_by_norad[id_a] if altitude_bands_by_norad is not None else (0.0, 0.0)
        for idx_b in range(idx_a + 1, n):
            id_b = sorted_ids[idx_b]
            if altitude_bands_by_norad is not None:
                hp_b, ha_b = altitude_bands_by_norad[id_b]
                if not altitude_bands_overlap(hp_a, ha_a, hp_b, ha_b, pad_km):
                    radial_skipped += 1
                    continue

            track_b = tracks_by_norad[id_b]
            by_time = {
                row["time"]: row
                for row in track_b
                if "time" in row and (row.get("r_teme_km") is not None or row.get("r_km") is not None)
            }
            min_miss: Optional[float] = None
            max_miss: Optional[float] = None
            tca_str: Optional[str] = None
            rel_speed_tca: Optional[float] = None
            max_rel_speed: Optional[float] = None
            shared_cnt = 0

            for row in track_a:
                t_str = row.get("time")
                if not t_str:
                    continue
                other = by_time.get(t_str)
                if other is None:
                    continue
                r1_raw = row.get("r_teme_km") if "r_teme_km" in row else row.get("r_km")
                r2_raw = other.get("r_teme_km") if "r_teme_km" in other else other.get("r_km")
                v1_raw = row.get("v_teme_km_s") if "v_teme_km_s" in row else row.get("v_km_s")
                v2_raw = other.get("v_teme_km_s") if "v_teme_km_s" in other else other.get("v_km_s")
                if r1_raw is None or r2_raw is None or v1_raw is None or v2_raw is None:
                    continue
                r1 = np.asarray(r1_raw, dtype=np.float64)
                r2 = np.asarray(r2_raw, dtype=np.float64)
                v1 = np.asarray(v1_raw, dtype=np.float64)
                v2 = np.asarray(v2_raw, dtype=np.float64)
                sep = float(np.linalg.norm(r1 - r2))
                rel_v = float(np.linalg.norm(v1 - v2))
                shared_cnt += 1
                if min_miss is None or sep < min_miss:
                    min_miss = sep
                    tca_str = str(t_str)
                    rel_speed_tca = rel_v
                if max_miss is None or sep > max_miss:
                    max_miss = sep
                if max_rel_speed is None or rel_v > max_rel_speed:
                    max_rel_speed = rel_v

            if min_miss is None or tca_str is None or max_miss is None or max_rel_speed is None:
                continue

            screened_pairs += 1
            name_a = str(names_by_norad[id_a]) if (names_by_norad and id_a in names_by_norad) else str(id_a)
            name_b = str(names_by_norad[id_b]) if (names_by_norad and id_b in names_by_norad) else str(id_b)
            pair_key = f"{id_a}-{id_b}"

            is_coloc = (
                coloc_dist > 0.0
                and coloc_speed > 0.0
                and max_miss <= coloc_dist
                and max_rel_speed <= coloc_speed
            )
            if is_coloc:
                classification = "co_located"
                co_located.append(
                    {
                        "pair": pair_key,
                        "classification": "co_located",
                        "primary_norad_id": id_a,
                        "primary_name": name_a,
                        "secondary_norad_id": id_b,
                        "secondary_name": name_b,
                        "tca": tca_str,
                        "min_miss_km": min_miss,
                        "geometric_miss_km": min_miss,
                        "max_miss_km": max_miss,
                        "max_separation_km": max_miss,
                        "relative_speed_km_s": rel_speed_tca,
                        "max_relative_speed_km_s": max_rel_speed,
                        "colocated_km": coloc_dist,
                        "colocated_rel_speed_km_s": coloc_speed,
                        "frame": frame,
                        "reason": colocated_reason,
                        "label": screening_label,
                        "neighbor_steps": shared_cnt,
                        "shared_steps": shared_cnt,
                    }
                )
            elif min_miss <= thresh:
                classification = "conjunction"
                conjunctions.append(
                    {
                        "pair": pair_key,
                        "classification": "conjunction",
                        "primary_norad_id": id_a,
                        "primary_name": name_a,
                        "secondary_norad_id": id_b,
                        "secondary_name": name_b,
                        "tca": tca_str,
                        "min_miss_km": min_miss,
                        "geometric_miss_km": min_miss,
                        "relative_speed_km_s": rel_speed_tca,
                        "threshold_km": thresh,
                        "frame": frame,
                        "label": screening_label,
                        "screening_kind": "geometric_screening",
                        "human_review_required": True,
                        "neighbor_steps": shared_cnt,
                        "shared_steps": shared_cnt,
                    }
                )
            else:
                classification = "clear"

            if populate_evaluated_pairs:
                evaluated_pairs[(id_a, id_b)] = {
                    "pair": pair_key,
                    "primary_norad_id": id_a,
                    "secondary_norad_id": id_b,
                    "tca": tca_str,
                    "min_miss_km": min_miss,
                    "max_miss_km": max_miss,
                    "relative_speed_km_s": rel_speed_tca,
                    "max_relative_speed_km_s": max_rel_speed,
                    "neighbor_steps": shared_cnt,
                    "shared_steps": shared_cnt,
                    "classification": classification,
                }

    return BroadphaseScreenResult(
        total_pair_count=total_pairs,
        screened_pair_count=screened_pairs,
        skipped_pair_count=total_pairs - screened_pairs,
        radial_skipped_pair_count=radial_skipped,
        spatial_skipped_pair_count=0,
        grid_km=thresh,
        threshold_km=thresh,
        colocated_km=coloc_dist,
        colocated_rel_speed_km_s=coloc_speed,
        conjunctions=conjunctions,
        co_located=co_located,
        evaluated_pairs=evaluated_pairs,
    )
