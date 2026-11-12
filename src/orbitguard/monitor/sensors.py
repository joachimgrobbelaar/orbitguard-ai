"""Sensor classes and ObservationSource plug-in interface for OrbitGuard.

Optical angles are not ranges. Cooperative satellite laser ranging and
uncooperative debris laser ranging are different, and both need an
external network. A conventional lidar does not map the LEO environment.

Only the public GP element-set source (:class:`CatalogElementSetSource`)
produces orbital element sets today. Hardware sensor sources
(:class:`LidarObservationSource`, :class:`VisionObservationSource`) are
explicitly ``not_connected`` stubs that document the measurement schema
and the required orbit-determination (OD) fusion step before any sensor
state could enter the tracking loop.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
import base64
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from orbitguard.monitor.constants import ALLOWED_PUBLIC_GROUP, CELESTRAK_STATIONS_URL, USER_AGENT
from orbitguard.monitor.ingest import (
    CatalogStore,
    HeaderTransport,
    HttpResult,
    Transport,
    default_header_transport,
    default_transport,
    ingest_validated_batch,
    invoke_header_transport,
    invoke_transport,
)
from orbitguard.monitor.provenance import age_seconds, as_utc, iso
from orbitguard.monitor.snapshot import load_stations_snapshot
from orbitguard.nd import RecordRejected, parse_epoch, validate_omm

DEFAULT_LOOP_FETCH_TIMEOUT_S = 8.0
DEFAULT_LOOP_GROUPS = (
    "stations",
    "active",
    "cosmos-1408-debris",
    "cosmos-2251-debris",
    "fengyun-1c-debris",
    "iridium-33-debris",
)
DEFAULT_GITHUB_OWNER = "joachimgrobbelaar"
DEFAULT_GITHUB_REPO = "orbitguard-ai"
DEFAULT_GITHUB_BRANCH = "master"
GITHUB_UNCONFIGURED_REASON = (
    "GitHub snapshot source is not configured "
    "(set ORBITGUARD_GITHUB_TOKEN or ORBITGUARD_GITHUB_PUBLIC=1)"
)
SNAPSHOT_ONLY_STATIONS_NOTE = (
    "Snapshot fallback covers only the 'stations' group (committed data/gp/stations.json); "
    "no committed snapshot exists for other CelesTrak groups."
)


class NotConnected(RuntimeError):
    """Raised when fetching from an observation source that is not connected."""


ObservationSourceNotConnected = NotConnected


class SourceStatus(dict):
    """Dictionary describing an ObservationSource status.

    Supports both dictionary access (``status()["status"] == "not_connected"``)
    and direct string equality (``status() == "not_connected"``).
    """

    def __eq__(self, other: object) -> bool:
        if isinstance(other, str):
            return self.get("status") == other
        return super().__eq__(other)


@dataclass
class ObservationBatch:
    """Batch of retrieved records with full source and timing provenance."""

    records: list[dict[str, Any]]
    source_name: str
    source_url: Optional[str]
    retrieved_at: Optional[str]
    source_mode: str  # "live", "snapshot_fallback", "not_connected"
    origin_flag: str = "live"  # "live", "snapshot", "sensor"
    live: bool = False
    snapshot: bool = False
    sensor: bool = False
    fallback_reason: Optional[str] = None
    snapshot_age_s: Optional[float] = None
    data_age_s: Optional[float] = None
    retrieved_by: Optional[str] = None
    fetched_at: Optional[str] = None
    note: Optional[str] = None
    groups: list[str] = field(default_factory=list)
    group_provenance: list[dict[str, Any]] = field(default_factory=list)
    deduplicated_count: int = 0
    snapshot_coverage_note: Optional[str] = None
    validated: list[dict[str, Any]] = field(default_factory=list)

    @property
    def provenance(self) -> dict[str, Any]:
        return {
            "source_name": self.source_name,
            "source_url": self.source_url,
            "retrieved_at": self.retrieved_at,
            "source_mode": self.source_mode,
            "origin_flag": self.origin_flag,
            "live": self.live,
            "snapshot": self.snapshot,
            "sensor": self.sensor,
            "fallback_reason": self.fallback_reason,
            "snapshot_age_s": self.snapshot_age_s,
            "data_age_s": self.data_age_s,
            "retrieved_by": self.retrieved_by,
            "fetched_at": self.fetched_at,
            "note": self.note,
            "groups": list(self.groups),
            "group_provenance": list(self.group_provenance),
            "deduplicated_count": self.deduplicated_count,
            "snapshot_coverage_note": self.snapshot_coverage_note,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "records": self.records,
            "object_count": len(self.records),
            **self.provenance,
        }


class ObservationSource(ABC):
    """Abstract plug-in interface for observation or element-set sources."""

    name: str
    kind: str

    @abstractmethod
    def status(self) -> SourceStatus:
        """Return operational status and provenance metadata for this source."""

    @abstractmethod
    def fetch(self, now: Optional[datetime] = None) -> ObservationBatch:
        """Retrieve observations or element sets with provenance."""


def _parse_iso(text: Optional[str]) -> Optional[datetime]:
    if not text:
        return None
    return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc)


def _excerpt(body: bytes, limit: int = 240) -> str:
    text = body.decode("utf-8", errors="replace").strip().replace("\n", " ")
    return text[:limit]


def _env_live_fetch_enabled(default: bool = True) -> bool:
    raw = os.environ.get("ORBITGUARD_LOOP_LIVE_FETCH")
    if raw is None:
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off"}


def _env_fetch_timeout_s(default: float = DEFAULT_LOOP_FETCH_TIMEOUT_S) -> float:
    raw = os.environ.get("ORBITGUARD_LOOP_FETCH_TIMEOUT_S")
    if raw is None or not raw.strip():
        return default
    try:
        val = float(raw)
        return max(0.5, val)
    except ValueError:
        return default


def configured_loop_groups(groups: Optional[Sequence[str] | str] = None) -> list[str]:
    """Return configured CelesTrak GP groups from argument or ``ORBITGUARD_LOOP_GROUPS``."""
    if groups is None:
        raw = os.environ.get("ORBITGUARD_LOOP_GROUPS", "")
        parts = raw.split(",")
    elif isinstance(groups, str):
        parts = groups.split(",")
    else:
        parts = []
        for item in groups:
            parts.extend(str(item).split(","))
    cleaned: list[str] = []
    seen: set[str] = set()
    for part in parts:
        g = part.strip()
        if g and g not in seen:
            seen.add(g)
            cleaned.append(g)
    return cleaned if cleaned else list(DEFAULT_LOOP_GROUPS)


def _env_github_token() -> Optional[str]:
    raw = os.environ.get("ORBITGUARD_GITHUB_TOKEN")
    if raw is None:
        return None
    val = raw.strip()
    return val if val else None


def _env_github_public(default: bool = False) -> bool:
    raw = os.environ.get("ORBITGUARD_GITHUB_PUBLIC")
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_github_owner(default: str = DEFAULT_GITHUB_OWNER) -> str:
    raw = os.environ.get("ORBITGUARD_GITHUB_OWNER")
    if raw is None or not raw.strip():
        return default
    return raw.strip()


def _env_github_repo(default: str = DEFAULT_GITHUB_REPO) -> str:
    raw = os.environ.get("ORBITGUARD_GITHUB_REPO")
    if raw is None or not raw.strip():
        return default
    return raw.strip()


def _env_github_branch(default: str = DEFAULT_GITHUB_BRANCH) -> str:
    raw = os.environ.get("ORBITGUARD_GITHUB_BRANCH")
    if raw is None or not raw.strip():
        return default
    return raw.strip()


def is_github_snapshot_configured(
    *,
    token: Optional[str] = None,
    public: Optional[bool] = None,
) -> bool:
    """Return True when a GitHub token or the public-raw flag is configured."""
    resolved_token = (token.strip() or None) if token is not None else _env_github_token()
    resolved_public = bool(public) if public is not None else _env_github_public()
    return bool(resolved_token or resolved_public)


def celestrak_group_url(group: str) -> str:
    """Return the CelesTrak GP OMM JSON URL for ``group``."""
    g = group.strip()
    if g == "stations":
        return CELESTRAK_STATIONS_URL
    return f"https://celestrak.org/NORAD/elements/gp.php?GROUP={g}&FORMAT=JSON"


class CatalogElementSetSource(ObservationSource):
    """CelesTrak GP element-set source with multi-group support and snapshot fallback.

    Groups are configurable via ``ORBITGUARD_LOOP_GROUPS`` (comma-separated,
    default ``"stations"``). Attempts a live CelesTrak GP OMM JSON download per
    group with a short timeout (default 8 s, configurable via
    ``ORBITGUARD_LOOP_FETCH_TIMEOUT_S``), merges and de-duplicates records by
    ``NORAD_CAT_ID``, and records per-group provenance.

    When ``ORBITGUARD_LOOP_LIVE_FETCH=0`` or when live requests fail, falls
    back to the committed ``data/gp/stations.json`` snapshot via
    :func:`load_stations_snapshot`. Snapshot fallback covers only the
    ``stations`` group; when fallback occurs for other groups, this is stated
    explicitly in the provenance. Stale snapshot data is never presented as live.
    """

    name = "celestrak_stations_gp"
    kind = "gp_element_set"

    def __init__(
        self,
        store: CatalogStore,
        *,
        groups: Optional[Sequence[str] | str] = None,
        timeout_s: Optional[float] = None,
        live_fetch: Optional[bool] = None,
        transport: Optional[Transport] = None,
        snapshot_dir: Optional[Path] = None,
    ) -> None:
        self.store = store
        self._groups = groups
        self._timeout_s = timeout_s
        self._live_fetch = live_fetch
        self._transport = transport
        self.snapshot_dir = snapshot_dir
        self._last_batch: Optional[ObservationBatch] = None

    @property
    def groups(self) -> list[str]:
        return configured_loop_groups(self._groups)

    @property
    def timeout_s(self) -> float:
        if self._timeout_s is not None:
            return max(0.5, float(self._timeout_s))
        return _env_fetch_timeout_s()

    @property
    def live_fetch_enabled(self) -> bool:
        if self._live_fetch is not None:
            return bool(self._live_fetch)
        return _env_live_fetch_enabled()

    def status(self) -> SourceStatus:
        last = self._last_batch
        current_status = "ready" if last is None else last.source_mode
        groups = self.groups
        default_url = (
            CELESTRAK_STATIONS_URL
            if groups == ["stations"]
            else ", ".join(celestrak_group_url(g) for g in groups)
        )
        return SourceStatus(
            name=self.name,
            kind=self.kind,
            status=current_status,
            connected=True,
            groups=groups if last is None else last.groups,
            group_provenance=[] if last is None else last.group_provenance,
            live_fetch_enabled=self.live_fetch_enabled,
            timeout_s=self.timeout_s,
            source_mode=None if last is None else last.source_mode,
            source_name=None if last is None else last.source_name,
            source_url=default_url if last is None else last.source_url,
            retrieved_at=None if last is None else last.retrieved_at,
            fallback_reason=None if last is None else last.fallback_reason,
            snapshot_age_s=None if last is None else last.snapshot_age_s,
            data_age_s=None if last is None else last.data_age_s,
            snapshot_coverage_note=None if last is None else last.snapshot_coverage_note,
            note=(
                "CelesTrak GP OMM JSON with automatic fallback to committed "
                "data/gp/stations.json snapshot (stations group only) on egress failure."
                if last is None
                else last.note
            ),
        )

    def fetch(self, now: Optional[datetime] = None) -> ObservationBatch:
        now = as_utc(now) or datetime.now(timezone.utc)
        groups = self.groups
        if not self.live_fetch_enabled:
            reason = "live_fetch_disabled (ORBITGUARD_LOOP_LIVE_FETCH=0)"
            batch = self._load_fallback_for_groups(
                now=now,
                groups=groups,
                group_failures={g: reason for g in groups},
            )
            self._last_batch = batch
            return batch

        timeout = self.timeout_s
        transport = self._transport or (
            lambda target_url: default_transport(target_url, timeout_s=timeout)
        )

        # Fetch each configured group and collect validated records + per-group provenance
        group_results: dict[str, dict[str, Any]] = {}
        group_failures: dict[str, str] = {}

        for group in groups:
            url = celestrak_group_url(group)
            result: HttpResult = invoke_transport(transport, url)
            if result.status != 200:
                excerpt = _excerpt(result.body)
                detail = f"HTTP {result.status}" + (f": {excerpt}" if excerpt else "")
                group_failures[group] = f"live_fetch_failed ({detail})"
                continue

            try:
                payload = json.loads(result.body.decode("utf-8"))
            except Exception as exc:  # noqa: BLE001
                group_failures[group] = f"live_fetch_invalid_json ({exc})"
                continue

            if not isinstance(payload, list) or not payload:
                group_failures[group] = "live_fetch_empty_or_non_list_payload"
                continue

            validated_list: list[dict[str, Any]] = []
            seen_hashes: set[str] = set()
            for raw in payload:
                try:
                    val = validate_omm(raw)
                except RecordRejected:
                    continue
                h = val["content_hash"]
                if h not in seen_hashes:
                    seen_hashes.add(h)
                    val_copy = dict(val)
                    val_copy["source_group"] = group
                    val_copy["source_mode"] = "live"
                    val_copy["source_name"] = "celestrak_gp_json"
                    val_copy["source_url"] = url
                    val_copy["retrieved_at"] = iso(now)
                    validated_list.append(val_copy)

            if not validated_list:
                group_failures[group] = "live_fetch_all_records_rejected"
                continue

            ingest_validated_batch(
                self.store,
                payload,
                retrieved_at=now,
                source_name="celestrak_gp_json",
                source_url=url,
            )
            if group == ALLOWED_PUBLIC_GROUP:
                data = self.store.load()
                data["latest_public"] = {
                    "group": ALLOWED_PUBLIC_GROUP,
                    "retrieved_at": iso(now),
                    "source_name": "celestrak_gp_json",
                    "source_url": url,
                    "hashes": [v["content_hash"] for v in validated_list],
                }
                self.store.save(data)

            group_results[group] = {
                "validated": validated_list,
                "url": url,
                "retrieved_at": iso(now),
            }

        # If no group succeeded live, fall back to the committed stations snapshot
        if not group_results:
            batch = self._load_fallback_for_groups(
                now=now,
                groups=groups,
                group_failures=group_failures,
            )
            self._last_batch = batch
            return batch

        # At least one group succeeded live; check if any configured group failed
        group_prov: list[dict[str, Any]] = []
        raw_items: list[dict[str, Any]] = []
        fallback_reasons: list[str] = []
        non_stations_failed: list[str] = []

        for group in groups:
            if group in group_results:
                res = group_results[group]
                v_list = res["validated"]
                raw_items.extend(v_list)
                group_prov.append(
                    {
                        "group": group,
                        "source_mode": "live",
                        "source_name": "celestrak_gp_json",
                        "source_url": res["url"],
                        "retrieved_at": res["retrieved_at"],
                        "live": True,
                        "snapshot": False,
                        "snapshot_covers_group": False,
                        "object_count": len(v_list),
                        "fallback_reason": None,
                        "snapshot_age_s": None,
                        "data_age_s": 0.0,
                        "note": f"Live CelesTrak GP OMM JSON download for group '{group}'.",
                    }
                )
            else:
                fail_reason = group_failures[group]
                if group == "stations":
                    snap_batch = self._load_stations_snapshot_batch(now=now, reason=fail_reason)
                    for v in snap_batch.validated:
                        v_copy = dict(v)
                        v_copy["source_group"] = "stations"
                        v_copy["source_mode"] = "snapshot_fallback"
                        v_copy["source_name"] = snap_batch.source_name
                        v_copy["source_url"] = snap_batch.source_url
                        v_copy["retrieved_at"] = snap_batch.retrieved_at
                        raw_items.append(v_copy)
                    fallback_reasons.append(f"stations: {fail_reason}")
                    group_prov.append(
                        {
                            "group": "stations",
                            "source_mode": "snapshot_fallback",
                            "source_name": snap_batch.source_name,
                            "source_url": snap_batch.source_url,
                            "retrieved_at": snap_batch.retrieved_at,
                            "live": False,
                            "snapshot": True,
                            "snapshot_covers_group": True,
                            "object_count": len(snap_batch.validated),
                            "fallback_reason": fail_reason,
                            "snapshot_age_s": snap_batch.snapshot_age_s,
                            "data_age_s": snap_batch.data_age_s,
                            "note": snap_batch.note,
                        }
                    )
                else:
                    non_stations_failed.append(group)
                    explicit_reason = (
                        f"{fail_reason}; snapshot fallback covers only the stations group "
                        f"(no committed snapshot for group '{group}')"
                    )
                    fallback_reasons.append(f"{group}: {explicit_reason}")
                    group_prov.append(
                        {
                            "group": group,
                            "source_mode": "snapshot_fallback",
                            "source_name": "celestrak_stations_snapshot",
                            "source_url": celestrak_group_url(group),
                            "retrieved_at": None,
                            "live": False,
                            "snapshot": True,
                            "snapshot_covers_group": False,
                            "object_count": 0,
                            "fallback_reason": explicit_reason,
                            "snapshot_age_s": None,
                            "data_age_s": None,
                            "note": (
                                f"Live fetch failed for group '{group}' ({fail_reason}). "
                                f"{SNAPSHOT_ONLY_STATIONS_NOTE}"
                            ),
                        }
                    )

        merged_validated, cleaned_records, dedup_count = _deduplicate_by_norad(raw_items)
        urls = [celestrak_group_url(g) for g in groups]
        combined_url = urls[0] if len(urls) == 1 else ", ".join(urls)
        coverage_note = SNAPSHOT_ONLY_STATIONS_NOTE if non_stations_failed else None
        combined_fallback_reason = "; ".join(fallback_reasons) if fallback_reasons else None

        note_parts = [
            f"Live CelesTrak GP OMM JSON download for group(s): {', '.join(group_results.keys())}."
        ]
        if dedup_count > 0:
            note_parts.append(f"De-duplicated {dedup_count} duplicate NORAD record(s) across groups.")
        if non_stations_failed:
            note_parts.append(
                f"Fallback occurred for group(s) {', '.join(non_stations_failed)}: "
                f"{SNAPSHOT_ONLY_STATIONS_NOTE}"
            )

        batch = ObservationBatch(
            records=cleaned_records,
            validated=merged_validated,
            source_name="celestrak_gp_json",
            source_url=combined_url,
            retrieved_at=iso(now),
            source_mode="live",
            origin_flag="live",
            live=True,
            snapshot=bool(fallback_reasons),
            sensor=False,
            fallback_reason=combined_fallback_reason,
            snapshot_age_s=None,
            data_age_s=0.0,
            retrieved_by="orbitguard_tracking_loop",
            fetched_at=iso(now),
            note=" ".join(note_parts),
            groups=groups,
            group_provenance=group_prov,
            deduplicated_count=dedup_count,
            snapshot_coverage_note=coverage_note,
        )
        self._last_batch = batch
        return batch

    def _load_stations_snapshot_batch(self, *, now: datetime, reason: str) -> ObservationBatch:
        snap = load_stations_snapshot(self.store, now=now, directory=self.snapshot_dir)
        retrieved_at_str = snap["retrieved_at"]
        retrieved_dt = _parse_iso(retrieved_at_str)
        snap_age_s = age_seconds(retrieved_dt, now)

        store_data = self.store.load()
        hashes = (store_data.get("latest_snapshot") or {}).get("hashes") or []
        validated_list: list[dict[str, Any]] = []
        cleaned_records: list[dict[str, Any]] = []
        for h in hashes:
            stored = store_data["records"].get(h)
            if stored is None:
                continue
            val_copy = dict(stored)
            val_copy["source_group"] = "stations"
            val_copy["source_mode"] = "snapshot_fallback"
            val_copy["source_name"] = str(snap.get("source_name") or "celestrak_stations_snapshot")
            val_copy["source_url"] = snap.get("source_url")
            val_copy["retrieved_at"] = retrieved_at_str
            validated_list.append(val_copy)
            cleaned_records.append(stored["record"])

        merged_validated, merged_records, dedup_count = _deduplicate_by_norad(validated_list)
        return ObservationBatch(
            records=merged_records,
            validated=merged_validated,
            source_name=str(snap.get("source_name") or "celestrak_stations_snapshot"),
            source_url=snap.get("source_url"),
            retrieved_at=retrieved_at_str,
            source_mode="snapshot_fallback",
            origin_flag="snapshot",
            live=False,
            snapshot=True,
            sensor=False,
            fallback_reason=reason,
            snapshot_age_s=snap_age_s,
            data_age_s=snap_age_s,
            retrieved_by=snap.get("retrieved_by"),
            fetched_at=iso(now),
            note=(
                f"Snapshot fallback ({reason}). Retrieved at {retrieved_at_str} "
                f"by {snap.get('retrieved_by') or 'snapshot job'}; "
                f"snapshot age {snap_age_s:.1f} s."
                if snap_age_s is not None
                else f"Snapshot fallback ({reason}). Retrieved at {retrieved_at_str}."
            ),
            groups=["stations"],
            deduplicated_count=dedup_count,
        )

    def _load_fallback_for_groups(
        self,
        *,
        now: datetime,
        groups: list[str],
        group_failures: dict[str, str],
    ) -> ObservationBatch:
        non_stations = [g for g in groups if g != "stations"]
        primary_reason = group_failures.get("stations") or next(iter(group_failures.values()), "live_fetch_failed")
        if non_stations:
            other_str = ", ".join(non_stations)
            full_reason = (
                f"{primary_reason}; snapshot fallback covers only the stations group "
                f"(requested non-stations group(s) not in committed snapshot: {other_str})"
            )
        else:
            full_reason = primary_reason

        base_batch = self._load_stations_snapshot_batch(now=now, reason=full_reason)
        group_prov: list[dict[str, Any]] = []
        for g in groups:
            g_reason = group_failures.get(g, primary_reason)
            if g == "stations":
                group_prov.append(
                    {
                        "group": "stations",
                        "source_mode": "snapshot_fallback",
                        "source_name": base_batch.source_name,
                        "source_url": base_batch.source_url,
                        "retrieved_at": base_batch.retrieved_at,
                        "live": False,
                        "snapshot": True,
                        "snapshot_covers_group": True,
                        "object_count": len(base_batch.records),
                        "fallback_reason": g_reason,
                        "snapshot_age_s": base_batch.snapshot_age_s,
                        "data_age_s": base_batch.data_age_s,
                        "note": base_batch.note,
                    }
                )
            else:
                explicit_g_reason = (
                    f"{g_reason}; snapshot fallback covers only the stations group "
                    f"(no committed snapshot for group '{g}')"
                )
                group_prov.append(
                    {
                        "group": g,
                        "source_mode": "snapshot_fallback",
                        "source_name": base_batch.source_name,
                        "source_url": celestrak_group_url(g),
                        "retrieved_at": base_batch.retrieved_at,
                        "live": False,
                        "snapshot": True,
                        "snapshot_covers_group": False,
                        "object_count": 0,
                        "fallback_reason": explicit_g_reason,
                        "snapshot_age_s": base_batch.snapshot_age_s,
                        "data_age_s": base_batch.data_age_s,
                        "note": (
                            f"Fallback for group '{g}' ({g_reason}): "
                            f"{SNAPSHOT_ONLY_STATIONS_NOTE}"
                        ),
                    }
                )

        base_batch.groups = list(groups)
        base_batch.group_provenance = group_prov
        if non_stations:
            base_batch.snapshot_coverage_note = (
                f"{SNAPSHOT_ONLY_STATIONS_NOTE} Requested non-stations group(s): {', '.join(non_stations)}."
            )
            base_batch.note = f"{base_batch.note} {base_batch.snapshot_coverage_note}"
        return base_batch

    def _load_fallback(self, *, now: datetime, reason: str) -> ObservationBatch:
        return self._load_fallback_for_groups(
            now=now,
            groups=self.groups,
            group_failures={g: reason for g in self.groups},
        )


class GitHubSnapshotSource(ObservationSource):
    """GitHub-hosted CelesTrak GP snapshot source reachable from Hugging Face Spaces.

    Fetches the committed ``data/gp/<group>.json`` and ``data/gp/<group>.meta.json``
    files produced by the scheduled GitHub Actions snapshotter. Every record is
    labelled honestly as a snapshot (``source_mode = "github_snapshot"``,
    ``origin_flag = "snapshot"``, ``live = False``, ``snapshot = True``) with
    ``retrieved_at``, ``snapshot_age_s``, and ``data_age_s`` computed from the
    GitHub Action's ``retrieved_at`` timestamp in ``<group>.meta.json`` — never
    from the runtime HTTP fetch time.

    Authentication modes:
    - ``token`` (``ORBITGUARD_GITHUB_TOKEN`` set): Uses the GitHub REST contents API
      ``https://api.github.com/repos/{owner}/{repo}/contents/{path}?ref={branch}``
      with ``Authorization: Bearer <token>`` and decodes the base64 ``content`` field.
      The token is never logged or exposed in status, provenance, or notes.
    - ``public_raw`` (no token, ``ORBITGUARD_GITHUB_PUBLIC=1``): Uses
      ``https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{path}`` without
      an ``Authorization`` header.
    - ``unconfigured`` (neither set): Performs no HTTP requests and immediately
      falls back to the bundled ``data/gp/stations.json`` snapshot.
    """

    name = "github_gp_snapshot"
    kind = "gp_element_set"

    def __init__(
        self,
        store: CatalogStore,
        *,
        groups: Optional[Sequence[str] | str] = None,
        timeout_s: Optional[float] = None,
        token: Optional[str] = None,
        public: Optional[bool] = None,
        owner: Optional[str] = None,
        repo: Optional[str] = None,
        branch: Optional[str] = None,
        transport: Optional[HeaderTransport] = None,
        snapshot_dir: Optional[Path] = None,
    ) -> None:
        self.store = store
        self._groups = groups
        self._timeout_s = timeout_s
        self._token = token
        self._public = public
        self._owner = owner
        self._repo = repo
        self._branch = branch
        self._transport = transport
        self.snapshot_dir = snapshot_dir
        self._last_batch: Optional[ObservationBatch] = None

    @property
    def _resolved_token(self) -> Optional[str]:
        if self._token is not None:
            val = self._token.strip()
            return val if val else None
        return _env_github_token()

    @property
    def public(self) -> bool:
        if self._public is not None:
            return bool(self._public)
        return _env_github_public()

    @property
    def configured(self) -> bool:
        return bool(self._resolved_token or self.public)

    @property
    def auth_mode(self) -> str:
        if self._resolved_token:
            return "token"
        if self.public:
            return "public_raw"
        return "unconfigured"

    @property
    def owner(self) -> str:
        if self._owner is not None and self._owner.strip():
            return self._owner.strip()
        return _env_github_owner()

    @property
    def repo(self) -> str:
        if self._repo is not None and self._repo.strip():
            return self._repo.strip()
        return _env_github_repo()

    @property
    def branch(self) -> str:
        if self._branch is not None and self._branch.strip():
            return self._branch.strip()
        return _env_github_branch()

    @property
    def groups(self) -> list[str]:
        return configured_loop_groups(self._groups)

    @property
    def timeout_s(self) -> float:
        if self._timeout_s is not None:
            return max(0.5, float(self._timeout_s))
        return _env_fetch_timeout_s()

    def _redact(self, text: Optional[str]) -> Optional[str]:
        if not text:
            return text
        tok = self._resolved_token
        if tok and tok in text:
            return text.replace(tok, "<redacted>")
        return text

    def github_contents_url(self, path: str) -> str:
        clean_path = path.lstrip("/")
        return (
            f"https://api.github.com/repos/{self.owner}/{self.repo}/contents/"
            f"{clean_path}?ref={self.branch}"
        )

    def github_raw_url(self, path: str) -> str:
        clean_path = path.lstrip("/")
        return (
            f"https://raw.githubusercontent.com/{self.owner}/{self.repo}/"
            f"{self.branch}/{clean_path}"
        )

    def github_file_url(self, path: str) -> str:
        if self.auth_mode == "token":
            return self.github_contents_url(path)
        return self.github_raw_url(path)

    def _request_headers(self) -> dict[str, str]:
        tok = self._resolved_token
        if tok:
            return {
                "User-Agent": USER_AGENT,
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {tok}",
            }
        return {
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
        }

    def _decode_response_json(self, body: bytes) -> Any:
        outer = json.loads(body.decode("utf-8"))
        if isinstance(outer, dict) and "content" in outer:
            raw_content = outer.get("content")
            if not isinstance(raw_content, str) or not raw_content.strip():
                raise ValueError("GitHub contents API payload has an empty 'content' field")
            decoded_bytes = base64.b64decode(raw_content)
            return json.loads(decoded_bytes.decode("utf-8"))
        return outer

    def status(self) -> SourceStatus:
        last = self._last_batch
        if last is None:
            current_status = "ready" if self.configured else "unconfigured"
        else:
            current_status = last.source_mode
        groups = self.groups
        default_urls = [self.github_file_url(f"data/gp/{g}.json") for g in groups]
        default_url = default_urls[0] if len(default_urls) == 1 else ", ".join(default_urls)
        return SourceStatus(
            name=self.name,
            kind=self.kind,
            status=current_status,
            connected=True,
            configured=self.configured,
            configuration_mode=self.auth_mode,
            auth_mode=self.auth_mode,
            token_configured=bool(self._resolved_token),
            public_raw_enabled=self.public,
            owner=self.owner,
            repo=self.repo,
            branch=self.branch,
            groups=groups if last is None else last.groups,
            group_provenance=[] if last is None else last.group_provenance,
            timeout_s=self.timeout_s,
            source_mode=None if last is None else last.source_mode,
            source_name=None if last is None else last.source_name,
            source_url=default_url if last is None else last.source_url,
            retrieved_at=None if last is None else last.retrieved_at,
            fallback_reason=None if last is None else self._redact(last.fallback_reason),
            snapshot_age_s=None if last is None else last.snapshot_age_s,
            data_age_s=None if last is None else last.data_age_s,
            snapshot_coverage_note=None if last is None else last.snapshot_coverage_note,
            credentials_exposed=False,
            note=(
                f"GitHub-committed CelesTrak GP snapshots from {self.owner}/{self.repo}@{self.branch} "
                f"(mode: {self.auth_mode}); falls back to bundled data/gp/stations.json when unavailable."
                if last is None
                else self._redact(last.note)
            ),
        )

    def fetch(self, now: Optional[datetime] = None) -> ObservationBatch:
        now = as_utc(now) or datetime.now(timezone.utc)
        groups = self.groups

        if not self.configured:
            batch = self._load_fallback_for_groups(
                now=now,
                groups=groups,
                group_failures={g: GITHUB_UNCONFIGURED_REASON for g in groups},
            )
            self._last_batch = batch
            return batch

        timeout = self.timeout_s
        headers = self._request_headers()
        transport: HeaderTransport = self._transport or (
            lambda target_url, req_headers=None: default_header_transport(
                target_url, headers=req_headers, timeout_s=timeout
            )
        )

        group_results: dict[str, dict[str, Any]] = {}
        group_failures: dict[str, str] = {}

        for group in groups:
            data_path = f"data/gp/{group}.json"
            meta_path = f"data/gp/{group}.meta.json"
            data_url = self.github_file_url(data_path)
            meta_url = self.github_file_url(meta_path)

            try:
                data_res = invoke_header_transport(transport, data_url, headers)
            except Exception as exc:  # noqa: BLE001
                group_failures[group] = self._redact(f"github_fetch_error ({exc})") or "github_fetch_error"
                continue

            if data_res.status != 200:
                excerpt = self._redact(_excerpt(data_res.body))
                detail = f"HTTP {data_res.status}" + (f": {excerpt}" if excerpt else "")
                group_failures[group] = f"github_fetch_failed ({detail})"
                continue

            try:
                payload = self._decode_response_json(data_res.body)
            except Exception as exc:  # noqa: BLE001
                err_txt = self._redact(str(exc))
                group_failures[group] = f"github_invalid_json ({err_txt})"
                continue

            if not isinstance(payload, list) or not payload:
                group_failures[group] = "github_empty_or_non_list_payload"
                continue

            try:
                meta_res = invoke_header_transport(transport, meta_url, headers)
            except Exception as exc:  # noqa: BLE001
                group_failures[group] = self._redact(f"github_meta_fetch_error ({exc})") or "github_meta_fetch_error"
                continue

            if meta_res.status != 200:
                excerpt = self._redact(_excerpt(meta_res.body))
                detail = f"HTTP {meta_res.status}" + (f": {excerpt}" if excerpt else "")
                group_failures[group] = f"github_meta_fetch_failed ({detail})"
                continue

            try:
                meta = self._decode_response_json(meta_res.body)
            except Exception as exc:  # noqa: BLE001
                err_txt = self._redact(str(exc))
                group_failures[group] = f"github_meta_invalid_json ({err_txt})"
                continue

            if not isinstance(meta, dict) or not meta.get("retrieved_at"):
                group_failures[group] = "github_meta_missing_retrieved_at"
                continue

            try:
                retrieved_dt = _parse_iso(str(meta["retrieved_at"]))
            except Exception:  # noqa: BLE001
                retrieved_dt = None
            if retrieved_dt is None:
                group_failures[group] = "github_meta_invalid_retrieved_at"
                continue

            group_retrieved_at = iso(retrieved_dt)
            group_age_s = age_seconds(retrieved_dt, now)
            group_source_name = str(
                meta.get("source_name") or f"celestrak_{group.replace('-', '_')}_snapshot"
            )
            group_source_url = str(meta.get("source_url") or celestrak_group_url(group))
            group_retrieved_by = str(meta.get("retrieved_by") or "github-actions")
            group_note = str(
                meta.get("note")
                or (
                    f"Snapshot of one CelesTrak {group}-group download committed to GitHub. "
                    "OrbitGuard did not contact CelesTrak when displaying this file."
                )
            )

            validated_list: list[dict[str, Any]] = []
            seen_hashes: set[str] = set()
            for raw in payload:
                try:
                    val = validate_omm(raw)
                except RecordRejected:
                    continue
                h = val["content_hash"]
                if h not in seen_hashes:
                    seen_hashes.add(h)
                    val_copy = dict(val)
                    val_copy["source_group"] = group
                    val_copy["source_mode"] = "github_snapshot"
                    val_copy["source_name"] = group_source_name
                    val_copy["source_url"] = group_source_url
                    val_copy["retrieved_at"] = group_retrieved_at
                    validated_list.append(val_copy)

            if not validated_list:
                group_failures[group] = "github_fetch_all_records_rejected"
                continue

            ingest_validated_batch(
                self.store,
                payload,
                retrieved_at=retrieved_dt,
                source_name=group_source_name,
                source_url=group_source_url,
            )

            group_results[group] = {
                "validated": validated_list,
                "source_name": group_source_name,
                "source_url": group_source_url,
                "github_url": data_url,
                "github_meta_url": meta_url,
                "retrieved_at": group_retrieved_at,
                "retrieved_dt": retrieved_dt,
                "retrieved_by": group_retrieved_by,
                "age_s": group_age_s,
                "note": group_note,
            }

        # If every requested group failed, fall back cleanly to the bundled stations snapshot
        if not group_results:
            batch = self._load_fallback_for_groups(
                now=now,
                groups=groups,
                group_failures=group_failures,
            )
            self._last_batch = batch
            return batch

        # At least one group succeeded from GitHub; merge and de-duplicate by NORAD_CAT_ID
        group_prov: list[dict[str, Any]] = []
        raw_items: list[dict[str, Any]] = []
        partial_failures: list[str] = []

        for group in groups:
            if group in group_results:
                res = group_results[group]
                v_list = res["validated"]
                raw_items.extend(v_list)
                group_prov.append(
                    {
                        "group": group,
                        "status": "ok",
                        "source_mode": "github_snapshot",
                        "source_name": res["source_name"],
                        "source_url": res["source_url"],
                        "github_url": res["github_url"],
                        "github_meta_url": res["github_meta_url"],
                        "retrieved_at": res["retrieved_at"],
                        "retrieved_by": res["retrieved_by"],
                        "live": False,
                        "snapshot": True,
                        "snapshot_covers_group": True,
                        "object_count": len(v_list),
                        "fallback_reason": None,
                        "snapshot_age_s": res["age_s"],
                        "data_age_s": res["age_s"],
                        "age_s": res["age_s"],
                        "note": res["note"],
                    }
                )
            else:
                fail_reason = self._redact(group_failures.get(group, "github_fetch_failed")) or "github_fetch_failed"
                partial_failures.append(f"{group}: {fail_reason}")
                group_prov.append(
                    {
                        "group": group,
                        "status": "error",
                        "source_mode": "github_snapshot_failed",
                        "source_name": f"celestrak_{group.replace('-', '_')}_snapshot",
                        "source_url": celestrak_group_url(group),
                        "github_url": self.github_file_url(f"data/gp/{group}.json"),
                        "github_meta_url": self.github_file_url(f"data/gp/{group}.meta.json"),
                        "retrieved_at": None,
                        "retrieved_by": None,
                        "live": False,
                        "snapshot": True,
                        "snapshot_covers_group": False,
                        "object_count": 0,
                        "error": fail_reason,
                        "fallback_reason": fail_reason,
                        "snapshot_age_s": None,
                        "data_age_s": None,
                        "age_s": None,
                        "note": f"GitHub snapshot fetch failed for group '{group}': {fail_reason}.",
                    }
                )

        merged_validated, cleaned_records, dedup_count = _deduplicate_by_norad(raw_items)

        # Compute batch retrieved_at and snapshot_age_s from the committed meta retrieved_at
        # (never from the moment of the GitHub HTTP fetch). When multiple groups have
        # different retrieved_at timestamps, use the oldest (most conservative age).
        oldest_dt = min(res["retrieved_dt"] for res in group_results.values())
        batch_retrieved_at = iso(oldest_dt)
        batch_snap_age_s = age_seconds(oldest_dt, now)

        source_urls = [res["source_url"] for res in group_results.values()]
        combined_source_url = source_urls[0] if len(source_urls) == 1 else ", ".join(source_urls)
        single_res = next(iter(group_results.values())) if len(group_results) == 1 else None
        batch_source_name = (
            single_res["source_name"] if single_res is not None else "github_celestrak_snapshot"
        )
        combined_fallback_reason = "; ".join(partial_failures) if partial_failures else None

        note_parts = [
            f"GitHub-committed CelesTrak GP snapshot(s) from {self.owner}/{self.repo}@{self.branch} "
            f"for group(s): {', '.join(group_results.keys())} (NOT a live CelesTrak download; "
            f"snapshot retrieved_at={batch_retrieved_at})."
        ]
        if dedup_count > 0:
            note_parts.append(f"De-duplicated {dedup_count} duplicate NORAD record(s) across groups.")
        if partial_failures:
            note_parts.append(f"Skipped failed group(s): {'; '.join(partial_failures)}.")

        batch = ObservationBatch(
            records=cleaned_records,
            validated=merged_validated,
            source_name=batch_source_name,
            source_url=combined_source_url,
            retrieved_at=batch_retrieved_at,
            source_mode="github_snapshot",
            origin_flag="snapshot",
            live=False,
            snapshot=True,
            sensor=False,
            fallback_reason=combined_fallback_reason,
            snapshot_age_s=batch_snap_age_s,
            data_age_s=batch_snap_age_s,
            retrieved_by="github-actions",
            fetched_at=iso(now),
            note=self._redact(" ".join(note_parts)),
            groups=groups,
            group_provenance=group_prov,
            deduplicated_count=dedup_count,
            snapshot_coverage_note=None,
        )
        self._last_batch = batch
        return batch

    def _load_stations_snapshot_batch(self, *, now: datetime, reason: str) -> ObservationBatch:
        clean_reason = self._redact(reason) or "github_snapshot_fallback"
        snap = load_stations_snapshot(self.store, now=now, directory=self.snapshot_dir)
        retrieved_at_str = snap["retrieved_at"]
        retrieved_dt = _parse_iso(retrieved_at_str)
        snap_age_s = age_seconds(retrieved_dt, now)

        store_data = self.store.load()
        hashes = (store_data.get("latest_snapshot") or {}).get("hashes") or []
        validated_list: list[dict[str, Any]] = []
        for h in hashes:
            stored = store_data["records"].get(h)
            if stored is None:
                continue
            val_copy = dict(stored)
            val_copy["source_group"] = "stations"
            val_copy["source_mode"] = "snapshot_fallback"
            val_copy["source_name"] = str(snap.get("source_name") or "celestrak_stations_snapshot")
            val_copy["source_url"] = snap.get("source_url")
            val_copy["retrieved_at"] = retrieved_at_str
            validated_list.append(val_copy)

        merged_validated, merged_records, dedup_count = _deduplicate_by_norad(validated_list)
        return ObservationBatch(
            records=merged_records,
            validated=merged_validated,
            source_name=str(snap.get("source_name") or "celestrak_stations_snapshot"),
            source_url=snap.get("source_url"),
            retrieved_at=retrieved_at_str,
            source_mode="snapshot_fallback",
            origin_flag="snapshot",
            live=False,
            snapshot=True,
            sensor=False,
            fallback_reason=clean_reason,
            snapshot_age_s=snap_age_s,
            data_age_s=snap_age_s,
            retrieved_by=snap.get("retrieved_by"),
            fetched_at=iso(now),
            note=(
                f"Bundled snapshot fallback ({clean_reason}). Retrieved at {retrieved_at_str} "
                f"by {snap.get('retrieved_by') or 'snapshot job'}; "
                f"snapshot age {snap_age_s:.1f} s."
                if snap_age_s is not None
                else f"Bundled snapshot fallback ({clean_reason}). Retrieved at {retrieved_at_str}."
            ),
            groups=["stations"],
            deduplicated_count=dedup_count,
        )

    def _load_fallback_for_groups(
        self,
        *,
        now: datetime,
        groups: list[str],
        group_failures: Mapping[str, str],
    ) -> ObservationBatch:
        non_stations = [g for g in groups if g != "stations"]
        unique_reasons = list(dict.fromkeys(self._redact(r) or "github_fetch_failed" for r in group_failures.values()))
        if len(unique_reasons) == 1:
            primary_reason = unique_reasons[0]
        else:
            primary_reason = "; ".join(
                f"{g}: {self._redact(r)}" for g, r in group_failures.items()
            )

        if non_stations:
            other_str = ", ".join(non_stations)
            full_reason = (
                f"{primary_reason}; snapshot fallback covers only the stations group "
                f"(requested non-stations group(s) not in bundled snapshot: {other_str})"
            )
        else:
            full_reason = primary_reason

        base_batch = self._load_stations_snapshot_batch(now=now, reason=full_reason)
        group_prov: list[dict[str, Any]] = []
        for g in groups:
            g_reason = self._redact(group_failures.get(g, primary_reason)) or primary_reason
            if g == "stations":
                group_prov.append(
                    {
                        "group": "stations",
                        "status": "fallback",
                        "source_mode": "snapshot_fallback",
                        "source_name": base_batch.source_name,
                        "source_url": base_batch.source_url,
                        "github_url": self.github_file_url("data/gp/stations.json"),
                        "github_meta_url": self.github_file_url("data/gp/stations.meta.json"),
                        "retrieved_at": base_batch.retrieved_at,
                        "live": False,
                        "snapshot": True,
                        "snapshot_covers_group": True,
                        "object_count": len(base_batch.records),
                        "fallback_reason": g_reason,
                        "snapshot_age_s": base_batch.snapshot_age_s,
                        "data_age_s": base_batch.data_age_s,
                        "age_s": base_batch.snapshot_age_s,
                        "note": base_batch.note,
                    }
                )
            else:
                explicit_g_reason = (
                    f"{g_reason}; snapshot fallback covers only the stations group "
                    f"(no bundled snapshot for group '{g}')"
                )
                group_prov.append(
                    {
                        "group": g,
                        "status": "fallback_unavailable",
                        "source_mode": "snapshot_fallback",
                        "source_name": base_batch.source_name,
                        "source_url": celestrak_group_url(g),
                        "github_url": self.github_file_url(f"data/gp/{g}.json"),
                        "github_meta_url": self.github_file_url(f"data/gp/{g}.meta.json"),
                        "retrieved_at": base_batch.retrieved_at,
                        "live": False,
                        "snapshot": True,
                        "snapshot_covers_group": False,
                        "object_count": 0,
                        "fallback_reason": explicit_g_reason,
                        "snapshot_age_s": base_batch.snapshot_age_s,
                        "data_age_s": base_batch.data_age_s,
                        "age_s": base_batch.snapshot_age_s,
                        "note": (
                            f"Fallback for group '{g}' ({g_reason}): "
                            f"{SNAPSHOT_ONLY_STATIONS_NOTE}"
                        ),
                    }
                )

        base_batch.groups = list(groups)
        base_batch.group_provenance = group_prov
        if non_stations:
            base_batch.snapshot_coverage_note = (
                f"{SNAPSHOT_ONLY_STATIONS_NOTE} Requested non-stations group(s): {', '.join(non_stations)}."
            )
            base_batch.note = f"{base_batch.note} {base_batch.snapshot_coverage_note}"
        return base_batch


def _deduplicate_by_norad(
    validated_items: Sequence[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int]:
    """Merge and de-duplicate validated OMM items by ``NORAD_CAT_ID``.

    When the same ``NORAD_CAT_ID`` appears multiple times (e.g. across
    overlapping CelesTrak groups), retains the element set with the newer
    ``EPOCH`` (or the first seen if epochs match) and records all groups in
    ``source_groups``.
    """
    by_norad: dict[int, dict[str, Any]] = {}
    order: list[int] = []
    dedup_count = 0

    for item in validated_items:
        rec = item["record"]
        nid = int(rec["NORAD_CAT_ID"])
        grp = item.get("source_group") or "stations"
        if nid not in by_norad:
            entry = dict(item)
            entry["source_groups"] = [grp]
            by_norad[nid] = entry
            order.append(nid)
        else:
            dedup_count += 1
            existing = by_norad[nid]
            groups_seen = list(existing.get("source_groups") or [])
            if grp not in groups_seen:
                groups_seen.append(grp)
            try:
                new_epoch = parse_epoch(str(rec["EPOCH"]))
                old_epoch = parse_epoch(str(existing["record"]["EPOCH"]))
                is_newer = new_epoch > old_epoch
            except Exception:  # noqa: BLE001
                is_newer = False
            if is_newer:
                replacement = dict(item)
                replacement["source_groups"] = groups_seen
                by_norad[nid] = replacement
            else:
                existing["source_groups"] = groups_seen

    merged_validated = [by_norad[nid] for nid in order]
    cleaned_records = [by_norad[nid]["record"] for nid in order]
    return merged_validated, cleaned_records, dedup_count


class LidarObservationSource(ObservationSource):
    """Disconnected stub for ground-based laser ranging / lidar observations.

    Status is always ``"not_connected"`` and :meth:`fetch` raises
    :class:`NotConnected` (never fabricating or returning synthetic data).

    Future hardware interface specification
    ---------------------------------------
    1. **Where observations originate**:
        A qualified cooperative Satellite Laser Ranging (SLR) station (e.g.
        ILRS network station outputting CRD normal points) or a licensed
        uncooperative debris-ranging terminal with site-specific aviation and
        laser-safety clearance.
    2. **Measurement record format**:
        Time-tagged topocentric range and pointing observations, NOT orbital
        elements::

            {
                "timestamp_utc": "YYYY-MM-DDTHH:MM:SS.ffffffZ",
                "station_id": "<station-code>",
                "site_wgs84": {"lat_deg": float, "lon_deg": float, "alt_m": float},
                "two_way_range_m": float,
                "range_rate_m_s": Optional[float],
                "azimuth_deg": Optional[float],
                "elevation_deg": Optional[float],
                "range_sigma_m": float,
                "angle_sigma_deg": Optional[float],
                "tropospheric_correction_m": float,
                "cooperative_retroreflector": bool,
            }

    3. **Required processing before entering TrackingLoop**:
        Raw slant ranges and angles cannot be passed to SGP4. They must first
        be ingested by an **Orbit Determination (OD)** pipeline (Initial Orbit
        Determination followed by batch weighted least-squares or a sequential
        Kalman filter) to solve for a fitted state vector and calibrated
        covariance (or fitted Brouwer mean elements) at a reference epoch.
    """

    name = "lidar"
    kind = "lidar"
    connected = False

    def status(self) -> SourceStatus:
        return SourceStatus(
            name=self.name,
            kind=self.kind,
            status="not_connected",
            connected=False,
            live=False,
            snapshot=False,
            sensor=True,
            observations_produced=0,
            expected_input_format=(
                "Time-tagged topocentric two-way range (m), optional range-rate (m/s) "
                "and azimuth/elevation (deg) + calibrated WGS-84 station site coordinates"
            ),
            pipeline_entry_point=(
                "Requires Orbit Determination (IOD + batch least-squares / Kalman OD) "
                "to fit a state vector or mean element set before entering TrackingLoop"
            ),
            note="Not connected. No lidar or SLR sensor hardware is attached; produces no data.",
        )

    def fetch(self, now: Optional[datetime] = None) -> ObservationBatch:
        raise NotConnected(
            "LidarObservationSource is not_connected: no lidar/SLR hardware is attached, "
            "and topocentric range/angle measurements require orbit determination before "
            "entering the tracking loop. No data are produced."
        )


class VisionObservationSource(ObservationSource):
    """Disconnected stub for optical/vision (``xvision``) astrometric observations.

    Status is always ``"not_connected"`` and :meth:`fetch` raises
    :class:`NotConnected` (never fabricating or returning synthetic data).

    Future hardware interface specification
    ---------------------------------------
    1. **Where observations originate**:
        Ground-based optical electro-optical telescope arrays or space-based
        star-tracker / vision cameras (``xvision``) producing plate-solved
        astrometric streak or point detections.
    2. **Measurement record format**:
        Angles-only line-of-sight tracklets and photometry (optical sensors do
        not measure slant range)::

            {
                "timestamp_utc": "YYYY-MM-DDTHH:MM:SS.ffffffZ",
                "sensor_id": "<xvision-sensor-id>",
                "observer_site_wgs84": {"lat_deg": float, "lon_deg": float, "alt_m": float},
                "frame": "ICRF",
                "ra_deg": float,
                "dec_deg": float,
                "ra_rate_deg_s": Optional[float],
                "dec_rate_deg_s": Optional[float],
                "angular_sigma_arcsec": float,
                "apparent_mag": Optional[float],
                "exposure_s": float,
            }

    3. **Required processing before entering TrackingLoop**:
        Angles-only line-of-sight observations lack range and cannot initialize
        a propagator directly. Tracklets must first go through **track
        association and angles-only Initial Orbit Determination (IOD)** (e.g.
        Gooding or Gauss multi-epoch solver) followed by **batch differential
        correction / sequential OD** across passes (or fusion with range data)
        before entering ``TrackingLoop``.
    """

    name = "xvision"
    kind = "vision"
    connected = False

    def status(self) -> SourceStatus:
        return SourceStatus(
            name=self.name,
            kind=self.kind,
            status="not_connected",
            connected=False,
            live=False,
            snapshot=False,
            sensor=True,
            observations_produced=0,
            expected_input_format=(
                "Time-tagged angles-only astrometric tracklets (RA/Dec or Az/El deg, "
                "angular rates, apparent magnitude, exposure timestamp) + observer site coordinates"
            ),
            pipeline_entry_point=(
                "Requires track association, angles-only IOD (e.g. Gooding/Gauss), and "
                "multi-pass Orbit Determination before entering TrackingLoop"
            ),
            note="Not connected. No optical/vision ('xvision') feed is attached; produces no data.",
        )

    def fetch(self, now: Optional[datetime] = None) -> ObservationBatch:
        raise NotConnected(
            "VisionObservationSource ('xvision') is not_connected: no optical/vision sensor "
            "is attached, and angles-only tracklets require orbit determination before "
            "entering the tracking loop. No data are produced."
        )


def sensor_catalog() -> dict[str, Any]:
    disconnected = "disconnected"
    return {
        "feed_status": disconnected,
        "mode_label": "No sensor feed is connected. Rows describe limits, not observations.",
        "tasking": (
            "This workspace does not identify surveillance targets and does not "
            "task, point, or fire a sensor or a laser."
        ),
        "sensors": [
            {
                "id": "optical_track",
                "name": "Optical angles and brightness",
                "status": disconnected,
                "measures": "Line of sight and brightness, when a telescope feed exists.",
                "does_not_measure": "Slant range. An optical detection is not a measured distance.",
                "current_values": None,
            },
            {
                "id": "cooperative_slr",
                "name": "Cooperative satellite laser ranging",
                "status": disconnected,
                "measures": "Two-way range to a satellite carrying retroreflectors, from a qualified station.",
                "does_not_measure": "Uncooperative debris. ILRS participation is an external network, not a local lidar sweep.",
                "references": ["https://ilrs.gsfc.nasa.gov/lw19/docs/2014/Papers/3009_Kirchner_paper.pdf"],
                "current_values": None,
            },
            {
                "id": "debris_laser_ranging",
                "name": "Uncooperative debris laser ranging",
                "status": "not_integrated",
                "measures": "Range to a non-cooperative object only with specialized equipment and a suitable target.",
                "does_not_measure": (
                    "A universal LEO debris map. Range and accuracy depend on the "
                    "station, the laser, the atmosphere, and the target. No single "
                    "distance or accuracy is stated here."
                ),
                "approvals": (
                    "Any real transmit activity needs the aviation and laser-safety "
                    "approvals that apply at the site, plus a partner who can actually "
                    "range the object. A U.S. Laser Clearinghouse process is not "
                    "treated as the rule for South African operations."
                ),
                "current_values": None,
            },
            {
                "id": "conventional_lidar",
                "name": "Conventional lidar",
                "status": disconnected,
                "measures": "Nothing in this build.",
                "does_not_measure": "The LEO environment as a whole. A mapping lidar is not an orbital catalog.",
                "current_values": None,
            },
            {
                "id": "gnss_tec",
                "name": "GNSS total electron content",
                "status": disconnected,
                "measures": "Integrated electron column along a signal path, in TECU, when a TEC product is connected.",
                "does_not_measure": "Local 3D plasma density.",
                "current_values": None,
            },
            {
                "id": "in_situ_plasma",
                "name": "In-situ plasma instrument",
                "status": disconnected,
                "measures": "Local charged-particle properties at a vehicle, when that vehicle's product is ingested.",
                "does_not_measure": "A column TEC value, and not thermospheric neutral density.",
                "current_values": None,
            },
        ],
    }
