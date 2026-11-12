"""Bundled CelesTrak stations snapshot.

The file is produced by the GitHub Actions job that downloads the stations
group. Loading it is a separate action from a live CelesTrak request. The
retrieval time stored in the metadata is the time of that download, not the
time this process reads the file.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from orbitguard.monitor.ingest import CatalogStore, ingest_validated_batch
from orbitguard.monitor.provenance import iso

SNAPSHOT_SOURCE = "celestrak_stations_snapshot"


class SnapshotUnavailable(FileNotFoundError):
    """The stations snapshot is not in this build."""


def snapshot_directory() -> Optional[Path]:
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "data" / "gp"
        if (candidate / "stations.meta.json").is_file() and (candidate / "stations.json").is_file():
            return candidate
    return None


def describe_stations_snapshot(now: Optional[datetime] = None, directory: Optional[Path] = None) -> dict[str, Any]:
    """Metadata only. Does not ingest records or contact CelesTrak."""
    now = now or datetime.now(timezone.utc)
    directory = directory if directory is not None else snapshot_directory()
    if directory is None:
        return {
            "available": False,
            "live_download": False,
            "note": "No stations snapshot is packaged with this build.",
        }
    meta = _read_meta(directory)
    retrieved = _parse(meta["retrieved_at"])
    return {
        "available": True,
        "live_download": False,
        "group": meta.get("group", "stations"),
        "source_name": meta.get("source_name", SNAPSHOT_SOURCE),
        "source_url": meta["source_url"],
        "retrieved_at": iso(retrieved),
        "retrieved_by": meta.get("retrieved_by"),
        "retrieval_age_s": (now - retrieved).total_seconds(),
        "object_count": meta.get("object_count"),
        "note": meta.get("note")
        or (
            "Snapshot of one CelesTrak stations download. "
            "This process has not substituted it for a live request."
        ),
    }


def load_stations_snapshot(
    store: CatalogStore,
    *,
    now: Optional[datetime] = None,
    directory: Optional[Path] = None,
) -> dict[str, Any]:
    """Store the packaged stations file under ``latest_snapshot``.

    Does not write ``latest_public`` and does not call CelesTrak.
    """
    now = now or datetime.now(timezone.utc)
    directory = directory if directory is not None else snapshot_directory()
    if directory is None:
        raise SnapshotUnavailable("No stations snapshot is packaged with this build")
    meta = _read_meta(directory)
    retrieved = _parse(meta["retrieved_at"])
    payload = json.loads((directory / "stations.json").read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError("Stations snapshot JSON was not a list")
    source_name = str(meta.get("source_name") or SNAPSHOT_SOURCE)
    source_url = str(meta["source_url"])
    report = ingest_validated_batch(
        store,
        payload,
        retrieved_at=retrieved,
        source_name=source_name,
        source_url=source_url,
    )
    data = store.load()
    wanted = []
    seen = set()
    from nanodynamics.elements import RecordRejected, validate_omm

    for raw in payload:
        try:
            validated = validate_omm(raw)
        except RecordRejected:
            continue
        if validated["content_hash"] not in seen and validated["content_hash"] in data["records"]:
            seen.add(validated["content_hash"])
            wanted.append(validated["content_hash"])
    data["latest_snapshot"] = {
        "group": meta.get("group", "stations"),
        "retrieved_at": iso(retrieved),
        "loaded_at": iso(now),
        "source_name": source_name,
        "source_url": source_url,
        "retrieved_by": meta.get("retrieved_by"),
        "note": meta.get("note"),
        "live_download": False,
        "hashes": wanted,
    }
    store.save(data)
    report.update(
        {
            "live_download": False,
            "network_request": False,
            "group": meta.get("group", "stations"),
            "retrieved_at": iso(retrieved),
            "loaded_at": iso(now),
            "source_name": source_name,
            "source_url": source_url,
            "retrieved_by": meta.get("retrieved_by"),
            "object_count": len(wanted),
            "credentials_exposed": False,
            "note": meta.get("note"),
        }
    )
    return report


def _read_meta(directory: Path) -> dict[str, Any]:
    meta_path = directory / "stations.meta.json"
    data_path = directory / "stations.json"
    if not meta_path.is_file() or not data_path.is_file():
        raise SnapshotUnavailable("Stations snapshot metadata or JSON is missing")
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    if not isinstance(meta, dict) or not meta.get("retrieved_at") or not meta.get("source_url"):
        raise ValueError("Stations snapshot metadata needs retrieved_at and source_url")
    return meta


def _parse(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc)
