"""Download configured CelesTrak GP groups sequentially and write labelled snapshots.

The schedule is every two hours. For each configured group independently, if the
existing snapshot is younger than MIN_INTERVAL_S (2 hours), this script skips
calling CelesTrak for that group unless --force is passed. One GROUP=<group>
request per group, never a per-object walk.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from typing import Any, Callable, Sequence
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
DEST = ROOT / "data" / "gp"
CELESTRAK_STATIONS_URL = "https://celestrak.org/NORAD/elements/gp.php?GROUP=stations&FORMAT=JSON"
URL = CELESTRAK_STATIONS_URL

GROUPS: list[str] = [
    "stations",
    "active",
    "cosmos-1408-debris",
    "cosmos-2251-debris",
    "fengyun-1c-debris",
    "iridium-33-debris",
]
SNAPSHOT_GROUPS = GROUPS
DEFAULT_GROUPS = GROUPS

MIN_INTERVAL_S = 2 * 3600
USER_AGENT = "OrbitGuard/0.1 (public read-only GP and SWPC; cached)"
NOTE = (
    "Snapshot of one CelesTrak stations-group download. "
    "OrbitGuard did not contact CelesTrak when it displays this file. "
    "The retrieval time is when this job saved the response."
)


def group_url(group: str) -> str:
    """Return the CelesTrak GP OMM JSON URL for ``group``."""
    g = group.strip()
    if g == "stations":
        return CELESTRAK_STATIONS_URL
    return f"https://celestrak.org/NORAD/elements/gp.php?GROUP={g}&FORMAT=JSON"


def group_source_name(group: str) -> str:
    """Return the snapshot source_name identifier for ``group``."""
    g = group.strip()
    return f"celestrak_{g.replace('-', '_')}_snapshot"


def group_note(group: str) -> str:
    """Return the honest provenance note for ``group``'s snapshot metadata."""
    g = group.strip()
    return (
        f"Snapshot of one CelesTrak {g}-group download. "
        "OrbitGuard did not contact CelesTrak when it displays this file. "
        "The retrieval time is when this job saved the response."
    )


def _existing_age_s(now: datetime, group: str = "stations", dest: Path | None = None) -> float | None:
    target_dir = dest if dest is not None else DEST
    meta_path = target_dir / f"{group}.meta.json"
    if not meta_path.is_file():
        return None
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        if not isinstance(meta, dict) or not meta.get("retrieved_at"):
            return None
        retrieved = datetime.fromisoformat(str(meta["retrieved_at"]).replace("Z", "+00:00"))
        if retrieved.tzinfo is None:
            retrieved = retrieved.replace(tzinfo=timezone.utc)
        return (now - retrieved).total_seconds()
    except Exception:  # noqa: BLE001
        return None


def fetch_group_snapshots(
    groups: Sequence[str] | None = None,
    *,
    dest: Path | None = None,
    now: datetime | None = None,
    force: bool = False,
    min_interval_s: float | None = None,
    timeout_s: float = 45.0,
    urlopen: Callable[..., Any] | None = None,
) -> int:
    """Fetch each configured CelesTrak group sequentially and write snapshot + meta files.

    Returns 0 if all groups were fresh (skipped) or at least one needed group was
    successfully written. Returns 1 if one or more groups needed a fetch and all
    requested fetches failed (nothing written).
    """
    target_groups = list(groups) if groups is not None else list(GROUPS)
    target_dir = dest if dest is not None else DEST
    now_utc = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    interval_s = float(min_interval_s if min_interval_s is not None else MIN_INTERVAL_S)
    opener = urlopen if urlopen is not None else urllib.request.urlopen

    attempted = 0
    written = 0
    skipped = 0
    failed = 0

    for raw_group in target_groups:
        group = raw_group.strip()
        if not group:
            continue

        age = _existing_age_s(now_utc, group=group, dest=target_dir)
        if age is not None and age < interval_s and not force:
            print(
                f"[{group}] Snapshot is {age:.0f}s old. Skipping. "
                f"CelesTrak is not called again inside {interval_s:.0f}s."
            )
            skipped += 1
            continue

        attempted += 1
        url = group_url(group)
        request = urllib.request.Request(
            url,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        )
        try:
            with opener(request, timeout=timeout_s) as response:
                body = response.read()
                status = int(getattr(response, "status", None) or response.getcode() or 200)
        except urllib.error.HTTPError as exc:
            print(f"[{group}] CelesTrak returned HTTP {exc.code}: {exc}", file=sys.stderr)
            failed += 1
            continue
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            print(f"[{group}] CelesTrak request failed: {exc}", file=sys.stderr)
            failed += 1
            continue
        except Exception as exc:  # noqa: BLE001
            print(f"[{group}] Unexpected error contacting CelesTrak: {exc}", file=sys.stderr)
            failed += 1
            continue

        if status != 200:
            print(f"[{group}] CelesTrak returned HTTP {status}", file=sys.stderr)
            failed += 1
            continue

        try:
            payload = json.loads(body.decode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            print(f"[{group}] CelesTrak response was not valid JSON: {exc}", file=sys.stderr)
            failed += 1
            continue

        if not isinstance(payload, list) or not payload:
            print(f"[{group}] CelesTrak response was not a non-empty list", file=sys.stderr)
            failed += 1
            continue

        target_dir.mkdir(parents=True, exist_ok=True)
        data_file = target_dir / f"{group}.json"
        tmp_data = data_file.with_suffix(".json.tmp")
        tmp_data.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        tmp_data.replace(data_file)

        retrieved_at_str = now_utc.isoformat().replace("+00:00", "Z")
        meta = {
            "group": group,
            "source_name": group_source_name(group),
            "source_url": url,
            "retrieved_at": retrieved_at_str,
            "retrieved_by": "github-actions",
            "object_count": len(payload),
            "live_download": False,
            "note": group_note(group),
        }
        meta_file = target_dir / f"{group}.meta.json"
        tmp_meta = meta_file.with_suffix(".json.tmp")
        tmp_meta.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
        tmp_meta.replace(meta_file)

        written += 1
        print(f"[{group}] Wrote {len(payload)} records retrieved at {retrieved_at_str}")

    if attempted == 0:
        return 0
    if written == 0:
        return 1
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    force = "--force" in args
    return fetch_group_snapshots(force=force)


if __name__ == "__main__":
    raise SystemExit(main())
