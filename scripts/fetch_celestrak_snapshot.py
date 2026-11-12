"""Download the CelesTrak stations group once and write a labelled snapshot.

The schedule is every two hours. If the existing snapshot is younger than
that, this script does not call CelesTrak again. One GROUP=stations request,
never a per-object walk.
"""

from __future__ import annotations

import json
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEST = ROOT / "data" / "gp"
URL = "https://celestrak.org/NORAD/elements/gp.php?GROUP=stations&FORMAT=JSON"
MIN_INTERVAL_S = 2 * 3600
USER_AGENT = "OrbitGuard/0.1 (public read-only GP and SWPC; cached)"
NOTE = (
    "Snapshot of one CelesTrak stations-group download. "
    "OrbitGuard did not contact CelesTrak when it displays this file. "
    "The retrieval time is when this job saved the response."
)


def _existing_age_s(now: datetime) -> float | None:
    meta_path = DEST / "stations.meta.json"
    if not meta_path.is_file():
        return None
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    retrieved = datetime.fromisoformat(str(meta["retrieved_at"]).replace("Z", "+00:00"))
    return (now - retrieved).total_seconds()


def main() -> int:
    force = "--force" in sys.argv
    now = datetime.now(timezone.utc)
    age = _existing_age_s(now)
    if age is not None and age < MIN_INTERVAL_S and not force:
        print(f"Snapshot is {age:.0f}s old. Skipping. CelesTrak is not called again inside {MIN_INTERVAL_S}s.")
        return 0
    request = urllib.request.Request(URL, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=45) as response:
        body = response.read()
        status = int(response.status)
    if status != 200:
        print(f"CelesTrak returned HTTP {status}", file=sys.stderr)
        return 1
    payload = json.loads(body.decode("utf-8"))
    if not isinstance(payload, list) or not payload:
        print("CelesTrak response was not a non-empty list", file=sys.stderr)
        return 1
    DEST.mkdir(parents=True, exist_ok=True)
    stations = DEST / "stations.json"
    temporary = stations.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    temporary.replace(stations)
    meta = {
        "group": "stations",
        "source_name": "celestrak_stations_snapshot",
        "source_url": URL,
        "retrieved_at": now.isoformat().replace("+00:00", "Z"),
        "retrieved_by": "github-actions",
        "object_count": len(payload),
        "live_download": False,
        "note": NOTE,
    }
    (DEST / "stations.meta.json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {len(payload)} stations retrieved at {meta['retrieved_at']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
