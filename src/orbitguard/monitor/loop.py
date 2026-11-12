"""Real-time LEO tracking loop engine: retrieve -> store -> propagate -> screen -> repeat.

Propagation uses NanoDynamics SGP4 via :mod:`orbitguard.nd`. Conjunction
screening computes sampled geometric miss distance in TEME and flags close
approaches for human review. Docked or attached vehicles sharing (or nearly
sharing) element sets are classified separately as ``co_located`` rather than
close approaches. No collision probability (Pc) or expected collision count
(Nc) is ever computed or output.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import logging
import math
import os
from pathlib import Path
import sqlite3
import threading
from typing import Any, Mapping, Optional, Sequence

import numpy as np

from orbitguard.monitor.broadphase import (
    altitude_bands_overlap,
    count_radial_skipped_pairs,
    resolve_grid_km,
    screen_tracks_broadphase,
)
from orbitguard.monitor.ingest import CatalogStore, HeaderTransport, Transport
from orbitguard.monitor.provenance import age_seconds, as_utc, iso
from orbitguard.monitor.risk import geometric_screen
from orbitguard.monitor.sensors import (
    DEFAULT_LOOP_FETCH_TIMEOUT_S,
    SNAPSHOT_ONLY_STATIONS_NOTE,
    CatalogElementSetSource,
    GitHubSnapshotSource,
    LidarObservationSource,
    NotConnected,
    ObservationBatch,
    ObservationSource,
    VisionObservationSource,
    _deduplicate_by_norad,
    configured_loop_groups,
    is_github_snapshot_configured,
)
from orbitguard.nd import (
    EARTH_MU_KM3_S2,
    EARTH_RADIUS_KM,
    GP_REFERENCE_FRAME,
    RecordRejected,
    parse_epoch,
    propagate_sgp4,
    satrec_from_omm,
    validate_omm,
)

logger = logging.getLogger("orbitguard.loop")

SCREENING_LABEL = (
    "geometric screening - miss distance from propagated GP states, "
    "no collision probability, for human review"
)

COLOCATED_REASON = (
    "near-identical propagated states over the whole horizon; "
    "likely docked/attached or sharing an element set - not a close approach"
)

DEFAULT_LOOP_STEP_S = 30.0
DEFAULT_LOOP_HORIZON_FRACTION = 1.0 / 8.0
DEFAULT_LOOP_THRESHOLD_KM = 10.0
DEFAULT_LOOP_GRID_KM = DEFAULT_LOOP_THRESHOLD_KM
DEFAULT_LOOP_COLOCATED_KM = 5.0
DEFAULT_LOOP_COLOCATED_REL_SPEED_KM_S = 0.01
DEFAULT_LOOP_MIN_CADENCE_S = 10.0
DEFAULT_LOOP_MAX_CADENCE_S = 3600.0
DEFAULT_LOOP_KEEP_ITERATIONS = 10
DEFAULT_LOOP_MAX_OBJECTS = 3000
LEO_MAX_PERIGEE_ALTITUDE_KM = 2000.0

PRIORITY_DEBRIS_GROUPS: tuple[str, ...] = (
    "cosmos-1408-debris",
    "cosmos-2251-debris",
    "fengyun-1c-debris",
    "iridium-33-debris",
)

CAP_POLICY_DESCRIPTION = (
    "stations first, then named debris groups (cosmos-1408-debris, cosmos-2251-debris, "
    "fengyun-1c-debris, iridium-33-debris), then remaining groups (active last), "
    "then NORAD_CAT_ID ascending"
)


def is_loop_enabled() -> bool:
    """Return True unless ``ORBITGUARD_LOOP_ENABLED`` is set to a disabled flag."""
    raw = os.environ.get("ORBITGUARD_LOOP_ENABLED", "1").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def _env_float(name: str, default: float, minimum: float = 0.0) -> float:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return max(minimum, float(raw))
    except ValueError:
        return default


def _env_optional_float(name: str) -> Optional[float]:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _env_int(name: str, default: int, minimum: int = 1) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return max(minimum, int(raw))
    except ValueError:
        return default


def _env_max_objects(default: int = DEFAULT_LOOP_MAX_OBJECTS) -> int:
    """Return configured max tracked LEO objects from ``ORBITGUARD_LOOP_MAX_OBJECTS`` (0 = unlimited)."""
    raw = os.environ.get("ORBITGUARD_LOOP_MAX_OBJECTS")
    if raw is None or not raw.strip():
        return default
    try:
        return max(0, int(raw.strip()))
    except ValueError:
        return default


def _parse_iso(text: Optional[str]) -> Optional[datetime]:
    if not text:
        return None
    return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc)


def orbital_period_minutes(mean_motion_rev_per_day: float) -> float:
    """Orbital period in minutes from MEAN_MOTION (1440 / n)."""
    n = float(mean_motion_rev_per_day)
    if n <= 0.0:
        raise ValueError("MEAN_MOTION must be positive")
    return 1440.0 / n


def orbital_period_seconds(mean_motion_rev_per_day: float) -> float:
    """Orbital period in seconds from MEAN_MOTION ((1440 / n) * 60)."""
    return orbital_period_minutes(mean_motion_rev_per_day) * 60.0


def perigee_apogee_altitude_km(
    mean_motion_rev_per_day: float,
    eccentricity: float,
) -> tuple[float, float]:
    """Compute (perigee_altitude_km, apogee_altitude_km) from MEAN_MOTION and ECCENTRICITY.

    Uses NanoDynamics Earth constants (or standard WGS-72 constants) exported
    via :mod:`orbitguard.nd`.
    """
    n_rev_day = float(mean_motion_rev_per_day)
    e = float(eccentricity)
    if n_rev_day <= 0.0:
        raise ValueError("MEAN_MOTION must be positive")
    if e < 0.0 or e >= 1.0:
        raise ValueError("ECCENTRICITY must be in [0, 1)")
    n_rad_s = n_rev_day * (2.0 * math.pi / 86400.0)
    semi_major_axis_km = (EARTH_MU_KM3_S2 / (n_rad_s * n_rad_s)) ** (1.0 / 3.0)
    perigee_km = semi_major_axis_km * (1.0 - e) - EARTH_RADIUS_KM
    apogee_km = semi_major_axis_km * (1.0 + e) - EARTH_RADIUS_KM
    return perigee_km, apogee_km


def perigee_altitude_km(mean_motion_rev_per_day: float, eccentricity: float) -> float:
    """Return perigee altitude in km from MEAN_MOTION (rev/day) and ECCENTRICITY."""
    return perigee_apogee_altitude_km(mean_motion_rev_per_day, eccentricity)[0]


def apogee_altitude_km(mean_motion_rev_per_day: float, eccentricity: float) -> float:
    """Return apogee altitude in km from MEAN_MOTION (rev/day) and ECCENTRICITY."""
    return perigee_apogee_altitude_km(mean_motion_rev_per_day, eccentricity)[1]


def is_leo_orbit(
    mean_motion_rev_per_day: float,
    eccentricity: float,
    max_perigee_km: float = LEO_MAX_PERIGEE_ALTITUDE_KM,
) -> bool:
    """Return True when perigee altitude is strictly below ``max_perigee_km`` (2000 km)."""
    hp_km, _ = perigee_apogee_altitude_km(mean_motion_rev_per_day, eccentricity)
    return hp_km < max_perigee_km


def _group_cap_priority(
    group_name: str,
    configured_groups: Sequence[str],
) -> tuple[int, int]:
    """Return ``(tier, sub_rank)`` so ``stations`` and named debris groups are never starved by ``active``."""
    g = str(group_name or "stations").strip()
    if g == "stations":
        return (0, 0)
    cfg_index = (
        configured_groups.index(g)
        if g in configured_groups
        else len(configured_groups)
    )
    if g in PRIORITY_DEBRIS_GROUPS or g.endswith("-debris"):
        debris_sub = (
            PRIORITY_DEBRIS_GROUPS.index(g)
            if g in PRIORITY_DEBRIS_GROUPS
            else len(PRIORITY_DEBRIS_GROUPS) + cfg_index
        )
        return (1, cfg_index if g in configured_groups else debris_sub)
    if g == "active":
        return (3, cfg_index)
    return (2, cfg_index)


def cap_sort_key(
    item: Mapping[str, Any],
    configured_groups: Sequence[str],
) -> tuple[int, int, int]:
    """Deterministic sort key for ``ORBITGUARD_LOOP_MAX_OBJECTS`` capping.

    Priority tiers:
    1. ``stations`` (tier 0)
    2. Named debris groups (``cosmos-1408-debris``, ``cosmos-2251-debris``,
       ``fengyun-1c-debris``, ``iridium-33-debris``, and any ``*-debris`` group)
    3. Any other non-``active`` groups in configured order
    4. ``active`` last (so ``active`` never starves ``stations`` or debris clouds)
    Within the same group priority, sorts by ``NORAD_CAT_ID`` ascending.
    """
    item_groups = [str(g) for g in (item.get("source_groups") or []) if g]
    primary_grp = str(item.get("source_group") or "stations")
    if primary_grp not in item_groups:
        item_groups.append(primary_grp)
    best_tier, best_sub = min(
        (_group_cap_priority(g, configured_groups) for g in item_groups),
        default=(3, 0),
    )
    return (best_tier, best_sub, int(item["record"]["NORAD_CAT_ID"]))


def _compute_data_and_snapshot_ages(
    *,
    source_mode: Optional[str],
    retrieved_at: Optional[str],
    stored_snapshot_age_s: Optional[float],
    now_utc: datetime,
) -> tuple[Optional[float], Optional[float]]:
    """Return ``(snapshot_age_s, data_age_s)`` at ``now_utc``.

    For live data, ``snapshot_age_s`` is always ``None`` and ``data_age_s`` is
    seconds between ``retrieved_at`` and ``now_utc``. For snapshots
    (``github_snapshot`` or ``snapshot_fallback``), ``snapshot_age_s`` and
    ``data_age_s`` are computed from the committed snapshot's ``retrieved_at``.
    """
    retrieved_dt = _parse_iso(retrieved_at)
    data_age_s = age_seconds(retrieved_dt, now_utc) if retrieved_dt is not None else None
    if source_mode == "live":
        return None, data_age_s
    if source_mode in {"snapshot_fallback", "github_snapshot"}:
        snap_age = data_age_s if data_age_s is not None else stored_snapshot_age_s
        return snap_age, data_age_s if data_age_s is not None else stored_snapshot_age_s
    return None, data_age_s


def _extract_groups_retrieved(
    group_prov: Sequence[Mapping[str, Any]],
    source_mode: Optional[str],
) -> list[str]:
    """Return the list of groups that actually returned records in an iteration."""
    retrieved: list[str] = []
    for gp in group_prov:
        grp = gp.get("group")
        if not grp:
            continue
        cnt = int(gp.get("object_count") or 0)
        gp_mode = gp.get("source_mode")
        if cnt > 0 and gp_mode in {"live", "github_snapshot"}:
            retrieved.append(str(grp))
        elif (
            cnt > 0
            and gp_mode == "snapshot_fallback"
            and gp.get("snapshot_covers_group", grp == "stations")
        ):
            retrieved.append(str(grp))
    if not retrieved and source_mode == "snapshot_fallback":
        return ["stations"]
    return retrieved


def _classify_data_source_origin(
    source_mode: Optional[str],
    catalog_source: Optional[ObservationSource],
) -> str:
    """Classify the active data source origin: ``'github'``, ``'celestrak'``, or ``'bundled_fallback'``."""
    if source_mode == "github_snapshot":
        return "github"
    if source_mode == "live":
        return "celestrak"
    if source_mode == "snapshot_fallback":
        return "bundled_fallback"
    if isinstance(catalog_source, GitHubSnapshotSource):
        return "github" if catalog_source.configured else "bundled_fallback"
    if isinstance(catalog_source, CatalogElementSetSource):
        return "celestrak" if catalog_source.live_fetch_enabled else "bundled_fallback"
    return "custom"


def _evaluate_pair_horizon(
    primary: Sequence[dict[str, Any]],
    secondary: Sequence[dict[str, Any]],
    *,
    frame: str,
    threshold_km: float,
) -> dict[str, Any]:
    """Evaluate pairwise separation and relative speed across the shared horizon."""
    screen_res = geometric_screen(
        primary,
        secondary,
        frame=frame,
        threshold_km=threshold_km,
    )
    by_time = {row["time"]: row for row in secondary if row.get("r_teme_km") or row.get("r_km")}
    max_miss_km: Optional[float] = None
    max_rel_speed_km_s: Optional[float] = None
    for row in primary:
        other = by_time.get(row["time"])
        if other is None:
            continue
        r_key = "r_teme_km" if "r_teme_km" in row else "r_km"
        if r_key not in other:
            continue
        r1 = np.asarray(row[r_key], dtype=np.float64)
        r2 = np.asarray(other[r_key], dtype=np.float64)
        v1 = np.asarray(row.get("v_teme_km_s") or row.get("v_km_s"), dtype=np.float64)
        v2 = np.asarray(other.get("v_teme_km_s") or other.get("v_km_s"), dtype=np.float64)
        sep = float(np.linalg.norm(r1 - r2))
        rel_v = float(np.linalg.norm(v1 - v2))
        if max_miss_km is None or sep > max_miss_km:
            max_miss_km = sep
        if max_rel_speed_km_s is None or rel_v > max_rel_speed_km_s:
            max_rel_speed_km_s = rel_v
    screen_res["max_miss_km"] = max_miss_km
    screen_res["max_relative_speed_km_s"] = max_rel_speed_km_s
    return screen_res


class TrackingLoop:
    """Continuous LEO tracking loop: retrieve -> store -> propagate -> screen -> repeat."""

    def __init__(
        self,
        cache_dir: Path,
        *,
        store: Optional[CatalogStore] = None,
        sources: Optional[Sequence[ObservationSource]] = None,
        transport: Optional[HeaderTransport] = None,
        snapshot_dir: Optional[Path] = None,
        groups: Optional[Sequence[str] | str] = None,
        max_objects: Optional[int] = None,
        github_token: Optional[str] = None,
        github_public: Optional[bool] = None,
        github_owner: Optional[str] = None,
        github_repo: Optional[str] = None,
        github_branch: Optional[str] = None,
        step_s: Optional[float] = None,
        horizon_fraction: float = DEFAULT_LOOP_HORIZON_FRACTION,
        threshold_km: Optional[float] = None,
        grid_km: Optional[float] = None,
        colocated_km: Optional[float] = None,
        colocated_rel_speed_km_s: Optional[float] = None,
        min_cadence_s: Optional[float] = None,
        max_cadence_s: Optional[float] = None,
        cadence_s: Optional[float] = None,
        keep_iterations: Optional[int] = None,
        fetch_timeout_s: Optional[float] = None,
        live_fetch: Optional[bool] = None,
    ) -> None:
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.cache_dir / "tracking_loop.sqlite"
        self.store = store or CatalogStore(self.cache_dir / "catalog.json")
        self._groups = groups
        self.max_objects = (
            max(0, int(max_objects))
            if max_objects is not None
            else _env_max_objects(DEFAULT_LOOP_MAX_OBJECTS)
        )

        self.step_s = (
            max(1.0, float(step_s))
            if step_s is not None
            else _env_float("ORBITGUARD_LOOP_STEP_S", DEFAULT_LOOP_STEP_S, minimum=1.0)
        )
        self.horizon_fraction = max(1e-4, float(horizon_fraction))
        self.threshold_km = (
            max(0.0, float(threshold_km))
            if threshold_km is not None
            else _env_float("ORBITGUARD_LOOP_THRESHOLD_KM", DEFAULT_LOOP_THRESHOLD_KM, minimum=0.0)
        )
        raw_grid_km = (
            float(grid_km)
            if grid_km is not None
            else _env_optional_float("ORBITGUARD_LOOP_GRID_KM")
        )
        self.grid_km = resolve_grid_km(raw_grid_km, self.threshold_km)
        self.colocated_km = (
            max(0.0, float(colocated_km))
            if colocated_km is not None
            else _env_float("ORBITGUARD_LOOP_COLOCATED_KM", DEFAULT_LOOP_COLOCATED_KM, minimum=0.0)
        )
        self.colocated_rel_speed_km_s = (
            max(0.0, float(colocated_rel_speed_km_s))
            if colocated_rel_speed_km_s is not None
            else _env_float(
                "ORBITGUARD_LOOP_COLOCATED_REL_SPEED_KM_S",
                DEFAULT_LOOP_COLOCATED_REL_SPEED_KM_S,
                minimum=0.0,
            )
        )
        self.min_cadence_s = (
            max(0.1, float(min_cadence_s))
            if min_cadence_s is not None
            else _env_float("ORBITGUARD_LOOP_MIN_CADENCE_S", DEFAULT_LOOP_MIN_CADENCE_S, minimum=0.1)
        )
        self.max_cadence_s = (
            max(self.min_cadence_s, float(max_cadence_s))
            if max_cadence_s is not None
            else max(
                self.min_cadence_s,
                _env_float("ORBITGUARD_LOOP_MAX_CADENCE_S", DEFAULT_LOOP_MAX_CADENCE_S, minimum=1.0),
            )
        )
        self._cadence_override_s = (
            float(cadence_s)
            if cadence_s is not None
            else _env_optional_float("ORBITGUARD_LOOP_CADENCE_S")
        )
        self.keep_iterations = (
            max(1, int(keep_iterations))
            if keep_iterations is not None
            else _env_int("ORBITGUARD_LOOP_KEEP_ITERATIONS", DEFAULT_LOOP_KEEP_ITERATIONS, minimum=1)
        )

        if sources is not None:
            self.sources: list[ObservationSource] = list(sources)
            self.catalog_source: Optional[ObservationSource] = (
                self.sources[0] if self.sources else None
            )
        else:
            if is_github_snapshot_configured(token=github_token, public=github_public):
                self.catalog_source = GitHubSnapshotSource(
                    self.store,
                    groups=groups,
                    timeout_s=fetch_timeout_s,
                    token=github_token,
                    public=github_public,
                    owner=github_owner,
                    repo=github_repo,
                    branch=github_branch,
                    transport=transport,
                    snapshot_dir=snapshot_dir,
                )
            else:
                self.catalog_source = CatalogElementSetSource(
                    self.store,
                    groups=groups,
                    timeout_s=fetch_timeout_s,
                    live_fetch=live_fetch,
                    transport=transport,
                    snapshot_dir=snapshot_dir,
                )
            self.lidar_source = LidarObservationSource()
            self.vision_source = VisionObservationSource()
            self.sources = [
                self.catalog_source,
                self.lidar_source,
                self.vision_source,
            ]

        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self.last_error: Optional[str] = None
        self._current_cadence_s: float = self._clamp_cadence(
            self._cadence_override_s if self._cadence_override_s is not None else 600.0
        )
        # In-memory cache of propagated tracks keyed by norad_id:
        # {norad_id: {"canonical_hash": str, "epoch": str, "horizon_start": datetime, "horizon_end": datetime, "track": list}}
        self._propagation_cache: dict[int, dict[str, Any]] = {}

        self._init_db()

    @property
    def groups(self) -> list[str]:
        if getattr(self, "catalog_source", None) is not None and hasattr(self.catalog_source, "groups"):
            return list(self.catalog_source.groups)  # type: ignore[union-attr]
        return configured_loop_groups(self._groups)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS iterations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    source_mode TEXT,
                    source_name TEXT,
                    source_url TEXT,
                    retrieved_at TEXT,
                    object_count INTEGER NOT NULL DEFAULT 0,
                    error TEXT,
                    fallback_reason TEXT,
                    snapshot_age_s REAL,
                    cadence_s REAL,
                    shortest_period_s REAL,
                    min_horizon_s REAL,
                    max_horizon_s REAL,
                    propagated_count INTEGER NOT NULL DEFAULT 0,
                    reused_count INTEGER NOT NULL DEFAULT 0,
                    conjunction_count INTEGER NOT NULL DEFAULT 0,
                    co_located_count INTEGER NOT NULL DEFAULT 0,
                    screened_pair_count INTEGER NOT NULL DEFAULT 0,
                    skipped_pair_count INTEGER NOT NULL DEFAULT 0,
                    radial_skipped_pair_count INTEGER NOT NULL DEFAULT 0,
                    spatial_skipped_pair_count INTEGER NOT NULL DEFAULT 0,
                    grid_km REAL,
                    non_leo_filtered_count INTEGER NOT NULL DEFAULT 0,
                    deduplicated_count INTEGER NOT NULL DEFAULT 0,
                    objects_capped INTEGER NOT NULL DEFAULT 0,
                    max_objects INTEGER,
                    objects_before_cap INTEGER NOT NULL DEFAULT 0,
                    objects_after_cap INTEGER NOT NULL DEFAULT 0,
                    cap_reason TEXT,
                    groups_json TEXT,
                    group_provenance_json TEXT,
                    snapshot_coverage_note TEXT
                );

                CREATE TABLE IF NOT EXISTS element_sets (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    iteration_id INTEGER NOT NULL,
                    norad_id INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    object_id TEXT,
                    epoch TEXT NOT NULL,
                    canonical_hash TEXT NOT NULL,
                    raw_omm_json TEXT NOT NULL,
                    mean_motion REAL,
                    eccentricity REAL,
                    perigee_altitude_km REAL,
                    apogee_altitude_km REAL,
                    period_s REAL,
                    horizon_s REAL,
                    horizon_start TEXT,
                    horizon_end TEXT,
                    reused_propagation INTEGER NOT NULL DEFAULT 0,
                    source_group TEXT,
                    source_mode TEXT,
                    source_name TEXT,
                    source_url TEXT,
                    retrieved_at TEXT,
                    FOREIGN KEY (iteration_id) REFERENCES iterations(id)
                );

                CREATE TABLE IF NOT EXISTS propagated_states (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    iteration_id INTEGER NOT NULL,
                    norad_id INTEGER NOT NULL,
                    t TEXT NOT NULL,
                    x REAL NOT NULL,
                    y REAL NOT NULL,
                    z REAL NOT NULL,
                    vx REAL NOT NULL,
                    vy REAL NOT NULL,
                    vz REAL NOT NULL,
                    frame TEXT NOT NULL,
                    latitude_deg REAL,
                    longitude_deg REAL,
                    altitude_km REAL,
                    FOREIGN KEY (iteration_id) REFERENCES iterations(id)
                );

                CREATE TABLE IF NOT EXISTS conjunction_screenings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    iteration_id INTEGER NOT NULL,
                    pair TEXT NOT NULL,
                    primary_norad_id INTEGER NOT NULL,
                    primary_name TEXT NOT NULL,
                    secondary_norad_id INTEGER NOT NULL,
                    secondary_name TEXT NOT NULL,
                    tca TEXT NOT NULL,
                    min_miss_km REAL NOT NULL,
                    relative_speed_km_s REAL,
                    threshold_km REAL NOT NULL,
                    frame TEXT NOT NULL,
                    label TEXT NOT NULL,
                    FOREIGN KEY (iteration_id) REFERENCES iterations(id)
                );

                CREATE TABLE IF NOT EXISTS co_located_pairs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    iteration_id INTEGER NOT NULL,
                    pair TEXT NOT NULL,
                    primary_norad_id INTEGER NOT NULL,
                    primary_name TEXT NOT NULL,
                    secondary_norad_id INTEGER NOT NULL,
                    secondary_name TEXT NOT NULL,
                    tca TEXT NOT NULL,
                    min_miss_km REAL NOT NULL,
                    max_miss_km REAL NOT NULL,
                    relative_speed_km_s REAL,
                    max_relative_speed_km_s REAL NOT NULL,
                    colocated_km REAL NOT NULL,
                    colocated_rel_speed_km_s REAL NOT NULL,
                    frame TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    label TEXT NOT NULL,
                    FOREIGN KEY (iteration_id) REFERENCES iterations(id)
                );

                CREATE INDEX IF NOT EXISTS idx_element_sets_iteration
                    ON element_sets(iteration_id, norad_id);
                CREATE INDEX IF NOT EXISTS idx_propagated_states_iteration
                    ON propagated_states(iteration_id, norad_id);
                CREATE INDEX IF NOT EXISTS idx_conjunctions_iteration
                    ON conjunction_screenings(iteration_id, min_miss_km);
                CREATE INDEX IF NOT EXISTS idx_colocated_iteration
                    ON co_located_pairs(iteration_id, min_miss_km);
                """
            )
            # Ensure new columns exist when opening a pre-existing SQLite file
            iter_cols = {
                r["name"] for r in conn.execute("PRAGMA table_info(iterations)").fetchall()
            }
            for col_name, col_def in (
                ("co_located_count", "INTEGER NOT NULL DEFAULT 0"),
                ("screened_pair_count", "INTEGER NOT NULL DEFAULT 0"),
                ("skipped_pair_count", "INTEGER NOT NULL DEFAULT 0"),
                ("radial_skipped_pair_count", "INTEGER NOT NULL DEFAULT 0"),
                ("spatial_skipped_pair_count", "INTEGER NOT NULL DEFAULT 0"),
                ("grid_km", "REAL"),
                ("non_leo_filtered_count", "INTEGER NOT NULL DEFAULT 0"),
                ("deduplicated_count", "INTEGER NOT NULL DEFAULT 0"),
                ("objects_capped", "INTEGER NOT NULL DEFAULT 0"),
                ("max_objects", "INTEGER"),
                ("objects_before_cap", "INTEGER NOT NULL DEFAULT 0"),
                ("objects_after_cap", "INTEGER NOT NULL DEFAULT 0"),
                ("cap_reason", "TEXT"),
                ("groups_json", "TEXT"),
                ("group_provenance_json", "TEXT"),
                ("snapshot_coverage_note", "TEXT"),
            ):
                if col_name not in iter_cols:
                    conn.execute(f"ALTER TABLE iterations ADD COLUMN {col_name} {col_def}")

            elem_cols = {
                r["name"] for r in conn.execute("PRAGMA table_info(element_sets)").fetchall()
            }
            for col_name, col_def in (
                ("eccentricity", "REAL"),
                ("perigee_altitude_km", "REAL"),
                ("apogee_altitude_km", "REAL"),
                ("source_group", "TEXT"),
            ):
                if col_name not in elem_cols:
                    conn.execute(f"ALTER TABLE element_sets ADD COLUMN {col_name} {col_def}")

    def _clamp_cadence(self, raw_cadence_s: float) -> float:
        return max(self.min_cadence_s, min(self.max_cadence_s, float(raw_cadence_s)))

    @property
    def running(self) -> bool:
        return bool(self._running and self._thread is not None and self._thread.is_alive())

    def start(self) -> bool:
        """Start the background loop thread if enabled and not already running."""
        with self._lock:
            if self.running:
                return True
            if not is_loop_enabled():
                return False
            self._stop_event.clear()
            self._running = True
            self._thread = threading.Thread(
                target=self._run_forever,
                name="orbitguard-tracking-loop",
                daemon=True,
            )
            self._thread.start()
            return True

    def stop(self, timeout_s: float = 5.0) -> None:
        """Signal the background loop thread to stop and wait for termination."""
        with self._lock:
            self._running = False
            self._stop_event.set()
            thread = self._thread
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=timeout_s)
        with self._lock:
            self._thread = None

    def _run_forever(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.run_once()
            except Exception as exc:  # noqa: BLE001 - defensive guard; run_once already records errors
                self.last_error = f"{type(exc).__name__}: {exc}"
                logger.exception("Unhandled error in tracking loop iteration")
            wait_s = self._current_cadence_s
            if self._stop_event.wait(timeout=wait_s):
                break
        with self._lock:
            self._running = False

    def _load_cached_object_from_db(self, norad_id: int) -> Optional[dict[str, Any]]:
        """Load the most recent propagation for ``norad_id`` from SQLite if not in memory."""
        with self._connect() as conn:
            elem_row = conn.execute(
                """
                SELECT iteration_id, canonical_hash, epoch, horizon_start, horizon_end
                FROM element_sets
                WHERE norad_id = ? AND horizon_start IS NOT NULL AND horizon_end IS NOT NULL
                ORDER BY iteration_id DESC
                LIMIT 1
                """,
                (norad_id,),
            ).fetchone()
            if elem_row is None:
                return None
            state_rows = conn.execute(
                """
                SELECT t, x, y, z, vx, vy, vz, frame, latitude_deg, longitude_deg, altitude_km
                FROM propagated_states
                WHERE iteration_id = ? AND norad_id = ?
                ORDER BY t ASC
                """,
                (elem_row["iteration_id"], norad_id),
            ).fetchall()
            if not state_rows:
                return None
            h_start = _parse_iso(elem_row["horizon_start"])
            h_end = _parse_iso(elem_row["horizon_end"])
            if h_start is None or h_end is None:
                return None
            track = [
                {
                    "time": r["t"],
                    "r_teme_km": [float(r["x"]), float(r["y"]), float(r["z"])],
                    "v_teme_km_s": [float(r["vx"]), float(r["vy"]), float(r["vz"])],
                    "latitude_deg": r["latitude_deg"],
                    "longitude_deg": r["longitude_deg"],
                    "altitude_km": r["altitude_km"],
                    "frame": r["frame"],
                }
                for r in state_rows
            ]
            return {
                "canonical_hash": elem_row["canonical_hash"],
                "epoch": elem_row["epoch"],
                "horizon_start": h_start,
                "horizon_end": h_end,
                "track": track,
            }

    def _get_reusable_track(
        self,
        *,
        norad_id: int,
        content_hash: str,
        epoch: str,
        now: datetime,
    ) -> Optional[list[dict[str, Any]]]:
        """Return cached propagated track if hash & epoch match and horizon still covers ``now``."""
        cached = self._propagation_cache.get(norad_id)
        if cached is None:
            cached = self._load_cached_object_from_db(norad_id)
            if cached is not None:
                self._propagation_cache[norad_id] = cached
        if cached is None:
            return None
        if cached["canonical_hash"] != content_hash or cached["epoch"] != epoch:
            return None
        h_start: datetime = cached["horizon_start"]
        h_end: datetime = cached["horizon_end"]
        # Horizon covers now only when now is within [h_start, h_end]
        if now < h_start or now > h_end:
            return None
        kept = []
        for sample in cached["track"]:
            sample_dt = _parse_iso(sample["time"])
            if sample_dt is not None and sample_dt >= now:
                kept.append(sample)
        if not kept:
            return None
        return kept

    def run_once(self, now: Optional[datetime] = None) -> dict[str, Any]:
        """Execute one synchronous iteration: retrieve -> store -> propagate -> screen -> prune."""
        with self._lock:
            now_utc = as_utc(now) or datetime.now(timezone.utc)
            started_at = iso(now_utc)
            with self._connect() as conn:
                cur = conn.execute(
                    "INSERT INTO iterations (started_at, object_count) VALUES (?, 0)",
                    (started_at,),
                )
                iteration_id = int(cur.lastrowid)

            try:
                batches: list[ObservationBatch] = []
                source_statuses: list[dict[str, Any]] = []
                for source in self.sources:
                    try:
                        batch = source.fetch(now=now_utc)
                        batches.append(batch)
                        source_statuses.append(dict(source.status()))
                    except NotConnected:
                        source_statuses.append(dict(source.status()))

                if not batches:
                    raise RuntimeError("No connected observation source produced an ObservationBatch")

                primary_batch = batches[0]
                raw_validated_items: list[dict[str, Any]] = []
                batch_dedup_count = 0
                for batch in batches:
                    batch_dedup_count += int(getattr(batch, "deduplicated_count", 0))
                    items = batch.validated if batch.validated else [
                        validate_omm(raw) for raw in batch.records
                    ]
                    for item in items:
                        item_copy = dict(item)
                        item_copy.setdefault("source_group", (batch.groups[0] if batch.groups else "stations"))
                        item_copy.setdefault("source_mode", batch.source_mode)
                        item_copy.setdefault("source_name", batch.source_name)
                        item_copy.setdefault("source_url", batch.source_url)
                        item_copy.setdefault("retrieved_at", batch.retrieved_at)
                        raw_validated_items.append(item_copy)

                # Merge and de-duplicate by NORAD_CAT_ID across all batches/groups
                deduped_items, _, cross_batch_dedup = _deduplicate_by_norad(raw_validated_items)
                total_dedup_count = batch_dedup_count + cross_batch_dedup

                # Filter to LEO (perigee altitude < 2000 km computed from MEAN_MOTION / ECCENTRICITY)
                validated_items: list[dict[str, Any]] = []
                non_leo_filtered_count = 0
                altitude_bands_by_norad: dict[int, tuple[float, float]] = {}

                for item in deduped_items:
                    rec = item["record"]
                    nid = int(rec["NORAD_CAT_ID"])
                    mean_motion = float(rec["MEAN_MOTION"])
                    eccentricity = float(rec["ECCENTRICITY"])
                    hp_km, ha_km = perigee_apogee_altitude_km(mean_motion, eccentricity)
                    if hp_km >= LEO_MAX_PERIGEE_ALTITUDE_KM:
                        non_leo_filtered_count += 1
                        continue
                    item_with_alt = dict(item)
                    item_with_alt["perigee_altitude_km"] = hp_km
                    item_with_alt["apogee_altitude_km"] = ha_km
                    validated_items.append(item_with_alt)
                    altitude_bands_by_norad[nid] = (hp_km, ha_km)

                # Apply deterministic max-objects cap (if ORBITGUARD_LOOP_MAX_OBJECTS > 0):
                # Keep all 'stations' objects first, then named debris groups
                # (cosmos-1408-debris, cosmos-2251-debris, fengyun-1c-debris, iridium-33-debris),
                # then remaining groups (active last), then by NORAD_CAT_ID ascending.
                objects_before_cap = len(validated_items)
                objects_capped = False
                cap_reason: Optional[str] = None
                max_objects_val: Optional[int] = self.max_objects if self.max_objects > 0 else None
                if self.max_objects > 0 and objects_before_cap > self.max_objects:
                    objects_capped = True
                    configured_grp_order = list(self.groups)
                    validated_items = sorted(
                        validated_items,
                        key=lambda item: cap_sort_key(item, configured_grp_order),
                    )[: self.max_objects]
                    kept_norads = {int(it["record"]["NORAD_CAT_ID"]) for it in validated_items}
                    altitude_bands_by_norad = {
                        nid_k: band
                        for nid_k, band in altitude_bands_by_norad.items()
                        if nid_k in kept_norads
                    }
                    cap_reason = (
                        f"Capped tracked LEO objects from {objects_before_cap} to "
                        f"{len(validated_items)} (ORBITGUARD_LOOP_MAX_OBJECTS={self.max_objects}; "
                        f"policy: {CAP_POLICY_DESCRIPTION})."
                    )
                objects_after_cap = len(validated_items)
                objects_dropped_by_cap = objects_before_cap - objects_after_cap

                # First check which objects can reuse their cached propagation
                reusable_tracks: dict[int, list[dict[str, Any]]] = {}
                reused_grid_start: Optional[datetime] = None
                for item in validated_items:
                    rec = item["record"]
                    nid = int(rec["NORAD_CAT_ID"])
                    reused = self._get_reusable_track(
                        norad_id=nid,
                        content_hash=item["content_hash"],
                        epoch=str(rec["EPOCH"]),
                        now=now_utc,
                    )
                    if reused is not None:
                        reusable_tracks[nid] = reused
                        if reused_grid_start is None:
                            reused_grid_start = _parse_iso(reused[0]["time"])

                # Align fresh propagations to the active common grid if any object reused cache
                prop_start_dt = reused_grid_start if reused_grid_start is not None else now_utc

                tracks_by_norad: dict[int, list[dict[str, Any]]] = {}
                names_by_norad: dict[int, str] = {}
                periods_s: list[float] = []
                horizons_s: list[float] = []
                propagated_count = 0
                reused_count = 0

                element_rows: list[tuple[Any, ...]] = []
                state_rows: list[tuple[Any, ...]] = []

                for item in validated_items:
                    rec = item["record"]
                    nid = int(rec["NORAD_CAT_ID"])
                    name = str(rec["OBJECT_NAME"])
                    obj_id = str(rec["OBJECT_ID"])
                    epoch = str(rec["EPOCH"])
                    c_hash = str(item["content_hash"])
                    raw_json = json.dumps(rec, sort_keys=True)
                    mean_motion = float(rec["MEAN_MOTION"])
                    eccentricity = float(rec["ECCENTRICITY"])
                    hp_km = float(item["perigee_altitude_km"])
                    ha_km = float(item["apogee_altitude_km"])
                    period_s = orbital_period_seconds(mean_motion)
                    horizon_s = period_s * self.horizon_fraction
                    periods_s.append(period_s)
                    horizons_s.append(horizon_s)
                    names_by_norad[nid] = name

                    if nid in reusable_tracks:
                        track = reusable_tracks[nid]
                        reused_flag = 1
                        reused_count += 1
                        h_start_str = track[0]["time"]
                        h_end_str = track[-1]["time"]
                    else:
                        reused_flag = 0
                        satellite = satrec_from_omm(rec)
                        prop = propagate_sgp4(satellite, prop_start_dt, horizon_s, self.step_s)
                        track = prop.get("track") or []
                        propagated_count += 1
                        if track:
                            h_start_str = track[0]["time"]
                            h_end_str = track[-1]["time"]
                            self._propagation_cache[nid] = {
                                "canonical_hash": c_hash,
                                "epoch": epoch,
                                "horizon_start": _parse_iso(h_start_str) or prop_start_dt,
                                "horizon_end": _parse_iso(h_end_str)
                                or (prop_start_dt + timedelta(seconds=horizon_s)),
                                "track": track,
                            }
                        else:
                            h_start_str = None
                            h_end_str = None

                    if track:
                        tracks_by_norad[nid] = track

                    item_source_group = str(item.get("source_group") or "stations")
                    item_source_mode = str(item.get("source_mode") or primary_batch.source_mode)
                    item_source_name = str(item.get("source_name") or primary_batch.source_name)
                    item_source_url = item.get("source_url") or primary_batch.source_url
                    item_retrieved_at = item.get("retrieved_at") or primary_batch.retrieved_at

                    element_rows.append(
                        (
                            iteration_id,
                            nid,
                            name,
                            obj_id,
                            epoch,
                            c_hash,
                            raw_json,
                            mean_motion,
                            eccentricity,
                            hp_km,
                            ha_km,
                            period_s,
                            horizon_s,
                            h_start_str,
                            h_end_str,
                            reused_flag,
                            item_source_group,
                            item_source_mode,
                            item_source_name,
                            item_source_url,
                            item_retrieved_at,
                        )
                    )

                    for sample in track:
                        r_km = sample["r_teme_km"]
                        v_km_s = sample["v_teme_km_s"]
                        state_rows.append(
                            (
                                iteration_id,
                                nid,
                                sample["time"],
                                float(r_km[0]),
                                float(r_km[1]),
                                float(r_km[2]),
                                float(v_km_s[0]),
                                float(v_km_s[1]),
                                float(v_km_s[2]),
                                GP_REFERENCE_FRAME,
                                sample.get("latitude_deg"),
                                sample.get("longitude_deg"),
                                sample.get("altitude_km"),
                            )
                        )

                shortest_period_s = min(periods_s) if periods_s else None
                min_horizon_s = min(horizons_s) if horizons_s else None
                max_horizon_s = max(horizons_s) if horizons_s else None

                # Loop iteration cadence = horizon length of shortest-period object (1/8 of period),
                # configurable and clamped to sensible bounds.
                raw_cadence = (
                    self._cadence_override_s
                    if self._cadence_override_s is not None
                    else (min_horizon_s if min_horizon_s is not None else 600.0)
                )
                cadence_s = self._clamp_cadence(raw_cadence)
                self._current_cadence_s = cadence_s

                # Two-layer broad-phase (apogee/perigee radial sieve + 3D spatial hash
                # across the 27-neighborhood) followed by exact Euclidean screening for survivors.
                bp_result = screen_tracks_broadphase(
                    tracks_by_norad,
                    altitude_bands_by_norad=altitude_bands_by_norad,
                    names_by_norad=names_by_norad,
                    threshold_km=self.threshold_km,
                    colocated_km=self.colocated_km,
                    colocated_rel_speed_km_s=self.colocated_rel_speed_km_s,
                    grid_km=self.grid_km,
                    frame=GP_REFERENCE_FRAME,
                    screening_label=SCREENING_LABEL,
                    colocated_reason=COLOCATED_REASON,
                )
                screened_pair_count = bp_result.screened_pair_count
                radial_skipped_pair_count = bp_result.radial_skipped_pair_count
                spatial_skipped_pair_count = bp_result.spatial_skipped_pair_count
                skipped_pair_count = bp_result.skipped_pair_count
                total_pair_count = bp_result.total_pair_count
                effective_grid_km = bp_result.grid_km

                conjunction_rows: list[tuple[Any, ...]] = [
                    (
                        iteration_id,
                        c["pair"],
                        int(c["primary_norad_id"]),
                        str(c["primary_name"]),
                        int(c["secondary_norad_id"]),
                        str(c["secondary_name"]),
                        str(c["tca"]),
                        float(c["min_miss_km"]),
                        None if c["relative_speed_km_s"] is None else float(c["relative_speed_km_s"]),
                        float(c["threshold_km"]),
                        str(c["frame"]),
                        str(c["label"]),
                    )
                    for c in bp_result.conjunctions
                ]
                colocated_rows: list[tuple[Any, ...]] = [
                    (
                        iteration_id,
                        c["pair"],
                        int(c["primary_norad_id"]),
                        str(c["primary_name"]),
                        int(c["secondary_norad_id"]),
                        str(c["secondary_name"]),
                        str(c["tca"]),
                        float(c["min_miss_km"]),
                        float(c["max_miss_km"]),
                        None if c["relative_speed_km_s"] is None else float(c["relative_speed_km_s"]),
                        float(c["max_relative_speed_km_s"]),
                        float(c["colocated_km"]),
                        float(c["colocated_rel_speed_km_s"]),
                        str(c["frame"]),
                        str(c["reason"]),
                        str(c["label"]),
                    )
                    for c in bp_result.co_located
                ]

                finished_dt = datetime.now(timezone.utc)
                if finished_dt < now_utc:
                    finished_dt = now_utc
                finished_at = iso(finished_dt)

                groups_list = list(primary_batch.groups) if primary_batch.groups else self.groups
                group_prov_list = list(primary_batch.group_provenance)
                snap_age_s, data_age_s = _compute_data_and_snapshot_ages(
                    source_mode=primary_batch.source_mode,
                    retrieved_at=primary_batch.retrieved_at,
                    stored_snapshot_age_s=primary_batch.snapshot_age_s,
                    now_utc=now_utc,
                )

                with self._connect() as conn:
                    conn.executemany(
                        """
                        INSERT INTO element_sets (
                            iteration_id, norad_id, name, object_id, epoch,
                            canonical_hash, raw_omm_json, mean_motion, eccentricity,
                            perigee_altitude_km, apogee_altitude_km, period_s,
                            horizon_s, horizon_start, horizon_end, reused_propagation,
                            source_group, source_mode, source_name, source_url, retrieved_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        element_rows,
                    )
                    conn.executemany(
                        """
                        INSERT INTO propagated_states (
                            iteration_id, norad_id, t, x, y, z, vx, vy, vz,
                            frame, latitude_deg, longitude_deg, altitude_km
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        state_rows,
                    )
                    conn.executemany(
                        """
                        INSERT INTO conjunction_screenings (
                            iteration_id, pair, primary_norad_id, primary_name,
                            secondary_norad_id, secondary_name, tca, min_miss_km,
                            relative_speed_km_s, threshold_km, frame, label
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        conjunction_rows,
                    )
                    conn.executemany(
                        """
                        INSERT INTO co_located_pairs (
                            iteration_id, pair, primary_norad_id, primary_name,
                            secondary_norad_id, secondary_name, tca, min_miss_km,
                            max_miss_km, relative_speed_km_s, max_relative_speed_km_s,
                            colocated_km, colocated_rel_speed_km_s, frame, reason, label
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        colocated_rows,
                    )
                    conn.execute(
                        """
                        UPDATE iterations
                        SET finished_at = ?,
                            source_mode = ?,
                            source_name = ?,
                            source_url = ?,
                            retrieved_at = ?,
                            object_count = ?,
                            error = NULL,
                            fallback_reason = ?,
                            snapshot_age_s = ?,
                            cadence_s = ?,
                            shortest_period_s = ?,
                            min_horizon_s = ?,
                            max_horizon_s = ?,
                            propagated_count = ?,
                            reused_count = ?,
                            conjunction_count = ?,
                            co_located_count = ?,
                            screened_pair_count = ?,
                            skipped_pair_count = ?,
                            radial_skipped_pair_count = ?,
                            spatial_skipped_pair_count = ?,
                            grid_km = ?,
                            non_leo_filtered_count = ?,
                            deduplicated_count = ?,
                            objects_capped = ?,
                            max_objects = ?,
                            objects_before_cap = ?,
                            objects_after_cap = ?,
                            cap_reason = ?,
                            groups_json = ?,
                            group_provenance_json = ?,
                            snapshot_coverage_note = ?
                        WHERE id = ?
                        """,
                        (
                            finished_at,
                            primary_batch.source_mode,
                            primary_batch.source_name,
                            primary_batch.source_url,
                            primary_batch.retrieved_at,
                            len(validated_items),
                            primary_batch.fallback_reason,
                            snap_age_s,
                            cadence_s,
                            shortest_period_s,
                            min_horizon_s,
                            max_horizon_s,
                            propagated_count,
                            reused_count,
                            len(conjunction_rows),
                            len(colocated_rows),
                            screened_pair_count,
                            skipped_pair_count,
                            radial_skipped_pair_count,
                            spatial_skipped_pair_count,
                            effective_grid_km,
                            non_leo_filtered_count,
                            total_dedup_count,
                            1 if objects_capped else 0,
                            max_objects_val,
                            objects_before_cap,
                            objects_after_cap,
                            cap_reason,
                            json.dumps(groups_list),
                            json.dumps(group_prov_list),
                            primary_batch.snapshot_coverage_note,
                            iteration_id,
                        ),
                    )
                    self._prune_old_iterations(conn)

                groups_retrieved = _extract_groups_retrieved(group_prov_list, primary_batch.source_mode)
                active_cat_source = getattr(self, "catalog_source", None)
                catalog_source_class = (
                    type(active_cat_source).__name__ if active_cat_source is not None else None
                )
                catalog_source_name = (
                    getattr(active_cat_source, "name", None) if active_cat_source is not None else None
                )
                data_origin = _classify_data_source_origin(primary_batch.source_mode, active_cat_source)
                cap_info = {
                    "objects_capped": objects_capped,
                    "max_objects": max_objects_val,
                    "cap": max_objects_val,
                    "objects_before_cap": objects_before_cap,
                    "objects_after_cap": objects_after_cap,
                    "objects_dropped_by_cap": objects_dropped_by_cap,
                    "reason": cap_reason,
                    "policy": CAP_POLICY_DESCRIPTION,
                }
                data_source_info = {
                    "class": catalog_source_class,
                    "name": primary_batch.source_name or catalog_source_name,
                    "plugin_name": catalog_source_name,
                    "origin": data_origin,
                    "source_mode": primary_batch.source_mode,
                    "configured": getattr(active_cat_source, "configured", True),
                    "auth_mode": getattr(active_cat_source, "auth_mode", None),
                }

                prov_dict = dict(primary_batch.provenance)
                prov_dict["snapshot_age_s"] = snap_age_s
                prov_dict["data_age_s"] = data_age_s
                prov_dict["groups"] = groups_list
                prov_dict["groups_requested"] = groups_list
                prov_dict["groups_retrieved"] = groups_retrieved
                prov_dict["group_provenance"] = group_prov_list
                prov_dict["deduplicated_count"] = total_dedup_count
                prov_dict["objects_capped"] = objects_capped
                prov_dict["max_objects"] = max_objects_val
                prov_dict["objects_before_cap"] = objects_before_cap
                prov_dict["objects_after_cap"] = objects_after_cap
                prov_dict["cap_reason"] = cap_reason
                prov_dict["screened_pair_count"] = screened_pair_count
                prov_dict["radial_skipped_pair_count"] = radial_skipped_pair_count
                prov_dict["spatial_skipped_pair_count"] = spatial_skipped_pair_count
                prov_dict["skipped_pair_count"] = skipped_pair_count
                prov_dict["total_pair_count"] = total_pair_count
                prov_dict["grid_km"] = effective_grid_km

                self.last_error = None
                return {
                    "iteration_id": iteration_id,
                    "status": "ok",
                    "started_at": started_at,
                    "finished_at": finished_at,
                    "source_mode": primary_batch.source_mode,
                    "source_name": primary_batch.source_name,
                    "source_url": primary_batch.source_url,
                    "retrieved_at": primary_batch.retrieved_at,
                    "fallback_reason": primary_batch.fallback_reason,
                    "snapshot_age_s": snap_age_s,
                    "data_age_s": data_age_s,
                    "data_source": data_source_info,
                    "catalog_source_class": catalog_source_class,
                    "catalog_source_name": catalog_source_name,
                    "data_source_origin": data_origin,
                    "groups": groups_list,
                    "groups_requested": groups_list,
                    "groups_retrieved": groups_retrieved,
                    "group_provenance": group_prov_list,
                    "snapshot_coverage_note": primary_batch.snapshot_coverage_note,
                    "object_count": len(validated_items),
                    "objects_capped": objects_capped,
                    "max_objects": max_objects_val,
                    "object_cap": max_objects_val,
                    "objects_before_cap": objects_before_cap,
                    "objects_after_cap": objects_after_cap,
                    "objects_dropped_by_cap": objects_dropped_by_cap,
                    "cap_reason": cap_reason,
                    "cap": cap_info,
                    "non_leo_filtered_count": non_leo_filtered_count,
                    "deduplicated_count": total_dedup_count,
                    "propagated_count": propagated_count,
                    "reused_count": reused_count,
                    "screened_pair_count": screened_pair_count,
                    "radial_skipped_pair_count": radial_skipped_pair_count,
                    "spatial_skipped_pair_count": spatial_skipped_pair_count,
                    "skipped_pair_count": skipped_pair_count,
                    "total_pair_count": total_pair_count,
                    "conjunction_count": len(conjunction_rows),
                    "co_located_count": len(colocated_rows),
                    "cadence_s": cadence_s,
                    "shortest_period_s": shortest_period_s,
                    "min_horizon_s": min_horizon_s,
                    "max_horizon_s": max_horizon_s,
                    "step_s": self.step_s,
                    "threshold_km": self.threshold_km,
                    "grid_km": effective_grid_km,
                    "colocated_km": self.colocated_km,
                    "colocated_rel_speed_km_s": self.colocated_rel_speed_km_s,
                    "label": SCREENING_LABEL,
                    "provenance": prov_dict,
                    "sources": source_statuses,
                    "error": None,
                }
            except Exception as exc:  # noqa: BLE001 - record iteration error and keep loop running
                err_msg = f"{type(exc).__name__}: {exc}"
                self.last_error = err_msg
                finished_at = iso(datetime.now(timezone.utc))
                with self._connect() as conn:
                    conn.execute(
                        "UPDATE iterations SET finished_at = ?, error = ? WHERE id = ?",
                        (finished_at, err_msg, iteration_id),
                    )
                    self._prune_old_iterations(conn)
                return {
                    "iteration_id": iteration_id,
                    "status": "error",
                    "started_at": started_at,
                    "finished_at": finished_at,
                    "source_mode": None,
                    "source_name": None,
                    "source_url": None,
                    "retrieved_at": None,
                    "snapshot_age_s": None,
                    "data_age_s": None,
                    "object_count": 0,
                    "propagated_count": 0,
                    "reused_count": 0,
                    "screened_pair_count": 0,
                    "radial_skipped_pair_count": 0,
                    "spatial_skipped_pair_count": 0,
                    "skipped_pair_count": 0,
                    "total_pair_count": 0,
                    "conjunction_count": 0,
                    "co_located_count": 0,
                    "grid_km": self.grid_km,
                    "cadence_s": self._current_cadence_s,
                    "label": SCREENING_LABEL,
                    "error": err_msg,
                }

    def _prune_old_iterations(self, conn: sqlite3.Connection) -> None:
        kept_rows = conn.execute(
            "SELECT id FROM iterations ORDER BY id DESC LIMIT ?",
            (self.keep_iterations,),
        ).fetchall()
        if not kept_rows:
            return
        min_kept_id = min(int(r["id"]) for r in kept_rows)
        conn.execute("DELETE FROM propagated_states WHERE iteration_id < ?", (min_kept_id,))
        conn.execute("DELETE FROM element_sets WHERE iteration_id < ?", (min_kept_id,))
        conn.execute("DELETE FROM conjunction_screenings WHERE iteration_id < ?", (min_kept_id,))
        conn.execute("DELETE FROM co_located_pairs WHERE iteration_id < ?", (min_kept_id,))
        conn.execute("DELETE FROM iterations WHERE id < ?", (min_kept_id,))

    def _db_row_counts(self, conn: sqlite3.Connection) -> dict[str, int]:
        return {
            "iterations": int(conn.execute("SELECT COUNT(*) FROM iterations").fetchone()[0]),
            "element_sets": int(conn.execute("SELECT COUNT(*) FROM element_sets").fetchone()[0]),
            "propagated_states": int(
                conn.execute("SELECT COUNT(*) FROM propagated_states").fetchone()[0]
            ),
            "conjunction_screenings": int(
                conn.execute("SELECT COUNT(*) FROM conjunction_screenings").fetchone()[0]
            ),
            "co_located_pairs": int(
                conn.execute("SELECT COUNT(*) FROM co_located_pairs").fetchone()[0]
            ),
        }

    def _build_iteration_provenance(
        self,
        row: Optional[sqlite3.Row | dict[str, Any]],
        now_utc: datetime,
    ) -> dict[str, Any]:
        max_objects_val: Optional[int] = self.max_objects if self.max_objects > 0 else None
        if row is None:
            return {
                "source_mode": None,
                "source_name": None,
                "source_url": None,
                "retrieved_at": None,
                "live": False,
                "snapshot": False,
                "sensor": False,
                "fallback_reason": None,
                "snapshot_age_s": None,
                "data_age_s": None,
                "groups": self.groups,
                "groups_requested": self.groups,
                "groups_retrieved": [],
                "group_provenance": [],
                "deduplicated_count": 0,
                "objects_capped": False,
                "max_objects": max_objects_val,
                "objects_before_cap": 0,
                "objects_after_cap": 0,
                "cap_reason": None,
                "screened_pair_count": 0,
                "radial_skipped_pair_count": 0,
                "spatial_skipped_pair_count": 0,
                "skipped_pair_count": 0,
                "total_pair_count": 0,
                "grid_km": self.grid_km,
                "snapshot_coverage_note": None,
            }

        r = dict(row)
        source_mode = r.get("source_mode")
        retrieved_at = r.get("retrieved_at")
        snap_age_s, data_age_s = _compute_data_and_snapshot_ages(
            source_mode=source_mode,
            retrieved_at=retrieved_at,
            stored_snapshot_age_s=r.get("snapshot_age_s"),
            now_utc=now_utc,
        )
        groups_list = (
            json.loads(r["groups_json"])
            if r.get("groups_json")
            else self.groups
        )
        raw_group_prov = (
            json.loads(r["group_provenance_json"])
            if r.get("group_provenance_json")
            else []
        )
        updated_group_prov: list[dict[str, Any]] = []
        for gp in raw_group_prov:
            gp_copy = dict(gp)
            g_snap_age, g_data_age = _compute_data_and_snapshot_ages(
                source_mode=gp_copy.get("source_mode"),
                retrieved_at=gp_copy.get("retrieved_at"),
                stored_snapshot_age_s=gp_copy.get("snapshot_age_s"),
                now_utc=now_utc,
            )
            gp_copy["snapshot_age_s"] = g_snap_age
            gp_copy["data_age_s"] = g_data_age
            if "age_s" in gp_copy:
                gp_copy["age_s"] = g_snap_age if g_snap_age is not None else g_data_age
            updated_group_prov.append(gp_copy)

        groups_retrieved = _extract_groups_retrieved(updated_group_prov, source_mode)
        non_stations = [g for g in groups_list if g != "stations"]
        coverage_note = r.get("snapshot_coverage_note")
        if coverage_note is None and source_mode == "snapshot_fallback" and non_stations:
            coverage_note = (
                f"{SNAPSHOT_ONLY_STATIONS_NOTE} Requested non-stations group(s): {', '.join(non_stations)}."
            )

        obj_cnt = int(r.get("object_count") or 0)
        objs_before = int(r.get("objects_before_cap") or obj_cnt)
        objs_after = int(r.get("objects_after_cap") or obj_cnt)
        iter_max_objs = r.get("max_objects") if r.get("max_objects") is not None else max_objects_val
        screened_pairs = int(r.get("screened_pair_count") or 0)
        skipped_pairs = int(r.get("skipped_pair_count") or 0)
        radial_skipped_pairs = int(r.get("radial_skipped_pair_count") or 0)
        spatial_skipped_pairs = int(r.get("spatial_skipped_pair_count") or 0)
        iter_grid_km = float(r["grid_km"]) if r.get("grid_km") is not None else self.grid_km

        return {
            "source_mode": source_mode,
            "source_name": r.get("source_name"),
            "source_url": r.get("source_url"),
            "retrieved_at": retrieved_at,
            "live": bool(source_mode == "live"),
            "snapshot": bool(
                source_mode in {"snapshot_fallback", "github_snapshot"}
                or any(gp.get("snapshot") for gp in updated_group_prov)
            ),
            "sensor": False,
            "fallback_reason": r.get("fallback_reason"),
            "snapshot_age_s": snap_age_s,
            "data_age_s": data_age_s,
            "groups": groups_list,
            "groups_requested": groups_list,
            "groups_retrieved": groups_retrieved,
            "group_provenance": updated_group_prov,
            "deduplicated_count": int(r.get("deduplicated_count") or 0),
            "objects_capped": bool(r.get("objects_capped")),
            "max_objects": iter_max_objs,
            "objects_before_cap": objs_before,
            "objects_after_cap": objs_after,
            "cap_reason": r.get("cap_reason"),
            "screened_pair_count": screened_pairs,
            "radial_skipped_pair_count": radial_skipped_pairs,
            "spatial_skipped_pair_count": spatial_skipped_pairs,
            "skipped_pair_count": skipped_pairs,
            "total_pair_count": screened_pairs + skipped_pairs,
            "grid_km": iter_grid_km,
            "snapshot_coverage_note": coverage_note,
        }

    def status(self, now: Optional[datetime] = None) -> dict[str, Any]:
        """Return loop operational status, cadence, horizon, provenance, sources, and DB counts."""
        now_utc = as_utc(now) or datetime.now(timezone.utc)
        with self._connect() as conn:
            counts = self._db_row_counts(conn)
            max_id_row = conn.execute("SELECT COALESCE(MAX(id), 0) FROM iterations").fetchone()
            total_iterations = int(max_id_row[0]) if max_id_row else 0
            last_row = conn.execute(
                "SELECT * FROM iterations ORDER BY id DESC LIMIT 1"
            ).fetchone()

        last_iter: Optional[dict[str, Any]] = dict(last_row) if last_row is not None else None
        source_mode = last_iter["source_mode"] if last_iter else None
        provenance = self._build_iteration_provenance(last_iter, now_utc)
        snap_age_s = provenance["snapshot_age_s"]
        data_age_s = provenance["data_age_s"]
        if last_iter is not None:
            last_iter["snapshot_age_s"] = snap_age_s
            last_iter["data_age_s"] = data_age_s

        cadence_s = (
            float(last_iter["cadence_s"])
            if (last_iter and last_iter["cadence_s"] is not None)
            else self._current_cadence_s
        )
        min_horizon_s = last_iter["min_horizon_s"] if last_iter else None
        max_horizon_s = last_iter["max_horizon_s"] if last_iter else None
        shortest_period_s = last_iter["shortest_period_s"] if last_iter else None
        tracked_object_count = int(last_iter.get("object_count") or 0) if last_iter else 0
        conj_count = int(last_iter.get("conjunction_count") or 0) if last_iter else 0
        coloc_count = int(last_iter.get("co_located_count") or 0) if last_iter else 0
        screened_pairs = int(last_iter.get("screened_pair_count") or 0) if last_iter else 0
        skipped_pairs = int(last_iter.get("skipped_pair_count") or 0) if last_iter else 0
        radial_skipped_pairs = (
            int(last_iter.get("radial_skipped_pair_count") or 0) if last_iter else 0
        )
        spatial_skipped_pairs = (
            int(last_iter.get("spatial_skipped_pair_count") or 0) if last_iter else 0
        )
        grid_km_val = (
            float(last_iter["grid_km"])
            if (last_iter and last_iter.get("grid_km") is not None)
            else self.grid_km
        )
        non_leo_filtered = int(last_iter.get("non_leo_filtered_count") or 0) if last_iter else 0
        dedup_count = int(last_iter.get("deduplicated_count") or 0) if last_iter else 0

        max_objects_val: Optional[int] = (
            last_iter["max_objects"]
            if (last_iter and last_iter.get("max_objects") is not None)
            else (self.max_objects if self.max_objects > 0 else None)
        )
        objects_capped = bool(last_iter.get("objects_capped")) if last_iter else False
        objects_before_cap = (
            int(last_iter.get("objects_before_cap") or tracked_object_count)
            if last_iter
            else 0
        )
        objects_after_cap = (
            int(last_iter.get("objects_after_cap") or tracked_object_count)
            if last_iter
            else 0
        )
        objects_dropped_by_cap = max(0, objects_before_cap - objects_after_cap)
        cap_reason = last_iter.get("cap_reason") if last_iter else None

        active_cat_source = getattr(self, "catalog_source", None)
        catalog_source_class = (
            type(active_cat_source).__name__ if active_cat_source is not None else None
        )
        catalog_source_name = (
            getattr(active_cat_source, "name", None) if active_cat_source is not None else None
        )
        data_origin = _classify_data_source_origin(source_mode, active_cat_source)
        source_name = last_iter.get("source_name") if last_iter else None
        source_url = last_iter.get("source_url") if last_iter else None
        retrieved_at = last_iter.get("retrieved_at") if last_iter else None
        fallback_reason = last_iter.get("fallback_reason") if last_iter else None

        return {
            "running": self.running,
            "enabled": is_loop_enabled(),
            "iteration_count": total_iterations,
            "iterations": total_iterations,
            "retained_iteration_count": counts["iterations"],
            "keep_iterations": self.keep_iterations,
            "last_iteration_id": last_iter["id"] if last_iter else None,
            "last_started_at": last_iter["started_at"] if last_iter else None,
            "last_finished_at": last_iter["finished_at"] if last_iter else None,
            "last_iteration": last_iter,
            "data_source": {
                "class": catalog_source_class,
                "name": source_name or catalog_source_name,
                "plugin_name": catalog_source_name,
                "origin": data_origin,
                "source_mode": source_mode,
                "configured": getattr(active_cat_source, "configured", True),
                "auth_mode": getattr(active_cat_source, "auth_mode", None),
            },
            "catalog_source_class": catalog_source_class,
            "catalog_source_name": catalog_source_name,
            "data_source_origin": data_origin,
            "source_mode": source_mode,
            "source_name": source_name,
            "source_url": source_url,
            "retrieved_at": retrieved_at,
            "fallback_reason": fallback_reason,
            "snapshot_age_s": snap_age_s,
            "data_age_s": data_age_s,
            "object_count": tracked_object_count,
            "objects_capped": objects_capped,
            "max_objects": max_objects_val,
            "object_cap": max_objects_val,
            "objects_before_cap": objects_before_cap,
            "objects_after_cap": objects_after_cap,
            "objects_dropped_by_cap": objects_dropped_by_cap,
            "cap_reason": cap_reason,
            "cap": {
                "objects_capped": objects_capped,
                "max_objects": max_objects_val,
                "cap": max_objects_val,
                "objects_before_cap": objects_before_cap,
                "objects_after_cap": objects_after_cap,
                "objects_dropped_by_cap": objects_dropped_by_cap,
                "reason": cap_reason,
                "policy": CAP_POLICY_DESCRIPTION,
            },
            "cadence_s": cadence_s,
            "min_cadence_s": self.min_cadence_s,
            "max_cadence_s": self.max_cadence_s,
            "cadence": {
                "cadence_s": cadence_s,
                "min_cadence_s": self.min_cadence_s,
                "max_cadence_s": self.max_cadence_s,
                "shortest_period_s": shortest_period_s,
                "formula": "1/8 of shortest tracked orbital period, clamped to [min_cadence_s, max_cadence_s]",
            },
            "horizon_s": min_horizon_s,
            "horizon_fraction": self.horizon_fraction,
            "step_s": self.step_s,
            "horizon": {
                "fraction_of_period": self.horizon_fraction,
                "min_horizon_s": min_horizon_s,
                "max_horizon_s": max_horizon_s,
                "step_s": self.step_s,
                "formula": "1/8 of each object's orbital period (period = 1440 / MEAN_MOTION minutes)",
            },
            "threshold_km": self.threshold_km,
            "grid_km": grid_km_val,
            "colocated_km": self.colocated_km,
            "colocated_rel_speed_km_s": self.colocated_rel_speed_km_s,
            "conjunction_count": conj_count,
            "co_located_count": coloc_count,
            "screened_pair_count": screened_pairs,
            "radial_skipped_pair_count": radial_skipped_pairs,
            "spatial_skipped_pair_count": spatial_skipped_pairs,
            "skipped_pair_count": skipped_pairs,
            "total_pair_count": screened_pairs + skipped_pairs,
            "non_leo_filtered_count": non_leo_filtered,
            "deduplicated_count": dedup_count,
            "groups": provenance["groups"],
            "groups_requested": provenance["groups_requested"],
            "groups_retrieved": provenance["groups_retrieved"],
            "group_provenance": provenance["group_provenance"],
            "provenance": provenance,
            "last_error": (last_iter["error"] if (last_iter and last_iter["error"]) else self.last_error),
            "sources": [dict(s.status()) for s in self.sources],
            "db_path": str(self.db_path),
            "db_row_counts": counts,
            "row_counts": counts,
            "label": SCREENING_LABEL,
        }

    def conjunctions(self, now: Optional[datetime] = None) -> dict[str, Any]:
        """Return latest geometric close-approach screening results and co-located assemblies."""
        now_utc = as_utc(now) or datetime.now(timezone.utc)
        with self._connect() as conn:
            last_row = conn.execute(
                "SELECT * FROM iterations WHERE error IS NULL ORDER BY id DESC LIMIT 1"
            ).fetchone()
            if last_row is None:
                empty_prov = self._build_iteration_provenance(None, now_utc)
                return {
                    "iteration_id": None,
                    "label": SCREENING_LABEL,
                    "screening_kind": "geometric_screening",
                    "threshold_km": self.threshold_km,
                    "grid_km": self.grid_km,
                    "colocated_km": self.colocated_km,
                    "colocated_rel_speed_km_s": self.colocated_rel_speed_km_s,
                    "source_mode": None,
                    "snapshot_age_s": None,
                    "data_age_s": None,
                    "provenance": empty_prov,
                    "screened_pair_count": 0,
                    "radial_skipped_pair_count": 0,
                    "spatial_skipped_pair_count": 0,
                    "skipped_pair_count": 0,
                    "total_pair_count": 0,
                    "count": 0,
                    "conjunction_count": 0,
                    "conjunctions": [],
                    "co_located_count": 0,
                    "co_located_reason": COLOCATED_REASON,
                    "co_located": [],
                }

            iter_id = int(last_row["id"])
            conj_rows = conn.execute(
                """
                SELECT * FROM conjunction_screenings
                WHERE iteration_id = ?
                ORDER BY min_miss_km ASC, primary_norad_id ASC, secondary_norad_id ASC
                """,
                (iter_id,),
            ).fetchall()
            coloc_rows = conn.execute(
                """
                SELECT * FROM co_located_pairs
                WHERE iteration_id = ?
                ORDER BY min_miss_km ASC, primary_norad_id ASC, secondary_norad_id ASC
                """,
                (iter_id,),
            ).fetchall()

        last_iter_dict = dict(last_row)
        source_mode = last_iter_dict["source_mode"]
        retrieved_at = last_iter_dict["retrieved_at"]
        provenance = self._build_iteration_provenance(last_iter_dict, now_utc)
        provenance["evaluated_at"] = last_iter_dict["finished_at"]
        snap_age_s = provenance["snapshot_age_s"]
        data_age_s = provenance["data_age_s"]
        screened_pairs = int(last_iter_dict.get("screened_pair_count") or 0)
        skipped_pairs = int(last_iter_dict.get("skipped_pair_count") or 0)
        radial_skipped_pairs = int(last_iter_dict.get("radial_skipped_pair_count") or 0)
        spatial_skipped_pairs = int(last_iter_dict.get("spatial_skipped_pair_count") or 0)
        grid_km_val = (
            float(last_iter_dict["grid_km"])
            if last_iter_dict.get("grid_km") is not None
            else self.grid_km
        )

        conjunction_items = [
            {
                "iteration_id": int(r["iteration_id"]),
                "pair": r["pair"],
                "classification": "conjunction",
                "primary_norad_id": int(r["primary_norad_id"]),
                "primary_name": r["primary_name"],
                "secondary_norad_id": int(r["secondary_norad_id"]),
                "secondary_name": r["secondary_name"],
                "tca": r["tca"],
                "min_miss_km": float(r["min_miss_km"]),
                "geometric_miss_km": float(r["min_miss_km"]),
                "relative_speed_km_s": (
                    None if r["relative_speed_km_s"] is None else float(r["relative_speed_km_s"])
                ),
                "threshold_km": float(r["threshold_km"]),
                "frame": r["frame"],
                "label": r["label"],
                "screening_kind": "geometric_screening",
                "human_review_required": True,
                "source_mode": source_mode,
                "source_name": last_row["source_name"],
                "source_url": last_row["source_url"],
                "retrieved_at": retrieved_at,
                "snapshot_age_s": snap_age_s,
                "data_age_s": data_age_s,
            }
            for r in conj_rows
        ]

        colocated_items = [
            {
                "iteration_id": int(r["iteration_id"]),
                "pair": r["pair"],
                "classification": "co_located",
                "primary_norad_id": int(r["primary_norad_id"]),
                "primary_name": r["primary_name"],
                "secondary_norad_id": int(r["secondary_norad_id"]),
                "secondary_name": r["secondary_name"],
                "tca": r["tca"],
                "min_miss_km": float(r["min_miss_km"]),
                "geometric_miss_km": float(r["min_miss_km"]),
                "max_miss_km": float(r["max_miss_km"]),
                "max_separation_km": float(r["max_miss_km"]),
                "relative_speed_km_s": (
                    None if r["relative_speed_km_s"] is None else float(r["relative_speed_km_s"])
                ),
                "max_relative_speed_km_s": float(r["max_relative_speed_km_s"]),
                "colocated_km": float(r["colocated_km"]),
                "colocated_rel_speed_km_s": float(r["colocated_rel_speed_km_s"]),
                "frame": r["frame"],
                "reason": r["reason"],
                "label": r["label"],
                "source_mode": source_mode,
                "source_name": last_row["source_name"],
                "source_url": last_row["source_url"],
                "retrieved_at": retrieved_at,
                "snapshot_age_s": snap_age_s,
                "data_age_s": data_age_s,
            }
            for r in coloc_rows
        ]

        return {
            "iteration_id": iter_id,
            "label": SCREENING_LABEL,
            "screening_kind": "geometric_screening",
            "threshold_km": self.threshold_km,
            "grid_km": grid_km_val,
            "colocated_km": self.colocated_km,
            "colocated_rel_speed_km_s": self.colocated_rel_speed_km_s,
            "source_mode": source_mode,
            "snapshot_age_s": snap_age_s,
            "data_age_s": data_age_s,
            "provenance": provenance,
            "screened_pair_count": screened_pairs,
            "radial_skipped_pair_count": radial_skipped_pairs,
            "spatial_skipped_pair_count": spatial_skipped_pairs,
            "skipped_pair_count": skipped_pairs,
            "total_pair_count": screened_pairs + skipped_pairs,
            "count": len(conjunction_items),
            "conjunction_count": len(conjunction_items),
            "conjunctions": conjunction_items,
            "co_located_count": len(colocated_items),
            "co_located_reason": COLOCATED_REASON,
            "co_located": colocated_items,
        }

    def objects(self, now: Optional[datetime] = None) -> dict[str, Any]:
        """Return latest tracked objects with epoch, epoch age (hours), provenance, and horizon."""
        now_utc = as_utc(now) or datetime.now(timezone.utc)
        with self._connect() as conn:
            last_row = conn.execute(
                "SELECT * FROM iterations WHERE error IS NULL ORDER BY id DESC LIMIT 1"
            ).fetchone()
            if last_row is None:
                empty_prov = self._build_iteration_provenance(None, now_utc)
                return {
                    "iteration_id": None,
                    "source_mode": None,
                    "snapshot_age_s": None,
                    "data_age_s": None,
                    "provenance": empty_prov,
                    "count": 0,
                    "objects": [],
                }

            iter_id = int(last_row["id"])
            elem_rows = conn.execute(
                """
                SELECT * FROM element_sets
                WHERE iteration_id = ?
                ORDER BY norad_id ASC
                """,
                (iter_id,),
            ).fetchall()

            state_counts = {
                int(r["norad_id"]): int(r["cnt"])
                for r in conn.execute(
                    """
                    SELECT norad_id, COUNT(*) AS cnt
                    FROM propagated_states
                    WHERE iteration_id = ?
                    GROUP BY norad_id
                    """,
                    (iter_id,),
                ).fetchall()
            }

            first_states: dict[int, dict[str, Any]] = {}
            for s in conn.execute(
                """
                SELECT norad_id, t, x, y, z, vx, vy, vz, frame, latitude_deg, longitude_deg, altitude_km
                FROM propagated_states
                WHERE iteration_id = ?
                ORDER BY norad_id ASC, t ASC
                """,
                (iter_id,),
            ).fetchall():
                nid = int(s["norad_id"])
                if nid not in first_states:
                    first_states[nid] = {
                        "t": s["t"],
                        "x": float(s["x"]),
                        "y": float(s["y"]),
                        "z": float(s["z"]),
                        "vx": float(s["vx"]),
                        "vy": float(s["vy"]),
                        "vz": float(s["vz"]),
                        "frame": s["frame"],
                        "latitude_deg": s["latitude_deg"],
                        "longitude_deg": s["longitude_deg"],
                        "altitude_km": s["altitude_km"],
                    }

        source_mode = last_row["source_mode"]
        provenance = self._build_iteration_provenance(last_row, now_utc)
        snap_age_s = provenance["snapshot_age_s"]
        data_age_s = provenance["data_age_s"]

        objects_list: list[dict[str, Any]] = []
        for row in elem_rows:
            r = dict(row)
            nid = int(r["norad_id"])
            period_s = float(r["period_s"]) if r.get("period_s") is not None else 0.0
            horizon_s = float(r["horizon_s"]) if r.get("horizon_s") is not None else 0.0
            sample_cnt = state_counts.get(nid, 0)
            reused = bool(r.get("reused_propagation"))
            epoch_str = str(r["epoch"])
            try:
                epoch_dt = parse_epoch(epoch_str)
                epoch_age_s = age_seconds(epoch_dt, now_utc)
            except Exception:  # noqa: BLE001
                epoch_age_s = age_seconds(_parse_iso(epoch_str), now_utc)
            epoch_age_hours = (epoch_age_s / 3600.0) if epoch_age_s is not None else None

            obj_source_mode = r.get("source_mode") or source_mode
            obj_retrieved_at = r.get("retrieved_at") or last_row["retrieved_at"]
            obj_snap_age_s, obj_data_age_s = _compute_data_and_snapshot_ages(
                source_mode=obj_source_mode,
                retrieved_at=obj_retrieved_at,
                stored_snapshot_age_s=last_row["snapshot_age_s"],
                now_utc=now_utc,
            )

            objects_list.append(
                {
                    "norad_id": nid,
                    "norad_cat_id": nid,
                    "name": r["name"],
                    "object_id": r["object_id"],
                    "epoch": epoch_str,
                    "epoch_age_hours": epoch_age_hours,
                    "epoch_age_h": epoch_age_hours,
                    "epoch_age_s": epoch_age_s,
                    "canonical_hash": r["canonical_hash"],
                    "mean_motion": float(r["mean_motion"]) if r.get("mean_motion") is not None else None,
                    "eccentricity": float(r["eccentricity"]) if r.get("eccentricity") is not None else None,
                    "perigee_altitude_km": (
                        float(r["perigee_altitude_km"]) if r.get("perigee_altitude_km") is not None else None
                    ),
                    "apogee_altitude_km": (
                        float(r["apogee_altitude_km"]) if r.get("apogee_altitude_km") is not None else None
                    ),
                    "period_s": period_s,
                    "period_minutes": period_s / 60.0,
                    "horizon_s": horizon_s,
                    "horizon_minutes": horizon_s / 60.0,
                    "horizon_start": r.get("horizon_start"),
                    "horizon_end": r.get("horizon_end"),
                    "propagated_horizon": {
                        "start": r.get("horizon_start"),
                        "end": r.get("horizon_end"),
                        "duration_s": horizon_s,
                        "duration_minutes": horizon_s / 60.0,
                        "fraction_of_period": self.horizon_fraction,
                        "step_s": self.step_s,
                        "sample_count": sample_cnt,
                        "frame": GP_REFERENCE_FRAME,
                        "reused_from_cache": reused,
                    },
                    "sample_count": sample_cnt,
                    "reused_propagation": reused,
                    "frame": GP_REFERENCE_FRAME,
                    "group": r.get("source_group") or "stations",
                    "source": r.get("source_name"),
                    "source_name": r.get("source_name"),
                    "source_url": r.get("source_url"),
                    "source_mode": obj_source_mode,
                    "retrieved_at": obj_retrieved_at,
                    "snapshot_age_s": obj_snap_age_s,
                    "data_age_s": obj_data_age_s,
                    "fallback_reason": last_row["fallback_reason"],
                    "latest_state": first_states.get(nid),
                }
            )

        return {
            "iteration_id": iter_id,
            "source_mode": source_mode,
            "snapshot_age_s": snap_age_s,
            "data_age_s": data_age_s,
            "provenance": provenance,
            "count": len(objects_list),
            "objects": objects_list,
        }
