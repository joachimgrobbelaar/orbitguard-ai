---
title: OrbitGuard
emoji: 🛰️
colorFrom: blue
colorTo: indigo
sdk: docker
app_port: 7860
pinned: false
license: mit
short_description: Human-reviewed LEO catalog monitoring
---

# OrbitGuard

OrbitGuard is a human-reviewed LEO monitoring workspace. It retrieves one
cached CelesTrak GP/OMM JSON batch (the `stations` group), asks NanoDynamics
to propagate those mean elements with SGP4, and shows NOAA SWPC F10.7 and
estimated planetary K when those files return. Geometric miss distance is
the only conjunction figure. Collision probability stays `not_computable`.
Hall Nc stays `not_implemented`. Nothing in this service commands a
spacecraft or points a sensor.

This repository is the OrbitGuard product. It is not a second copy of the
NanoDynamics simulator.

## How the two repositories relate

```
OrbitGuard  -->  NanoDynamics
```

OrbitGuard installs NanoDynamics and imports it. NanoDynamics does not
import OrbitGuard, and it does not ship this UI.

| Concern | Where it lives |
| --- | --- |
| CelesTrak caching, provenance, NOAA indices, geometric screen, operator page | OrbitGuard (`src/orbitguard/monitor/`) |
| SGP4 wrapper, GMST display transform, Alpha-5 and TLE checksums, physical constants, numerical propagator | NanoDynamics (`nanodynamics.propagation`, `nanodynamics.frames`, `nanodynamics.elements`, `nanodynamics.constants`) |

Call sites import `nanodynamics` directly. `orbitguard.nd` records the pin
and re-exports the same symbols. OrbitGuard does not vendor that physics.

### Pin

NanoDynamics 1.1.0, dependencies numpy, scipy, and sgp4. The same line is
in `requirements.txt` and `src/orbitguard/nd.py`:

```text
nanodynamics @ git+https://github.com/joachimgrobbelaar/NanoDynamics@d22c000a304e4484ce58461afde31981647483bf
```

| Module | Used for |
| --- | --- |
| `nanodynamics.elements` | `validate_omm`, `canonical_hash`, Alpha-5, TLE checksums |
| `nanodynamics.frames` | GMST, TEME display rotation, `ensure_utc` |
| `nanodynamics.propagation` | SGP4, osculating numerical propagation (rejects GP/TLE/TEME), two-body check |
| `nanodynamics.constants` | Frame names, WGS-72 label, GMST at J2000 |

`/health` reports package version `1.1.0` and this pin.

## What is implemented

- One public CelesTrak request for `GROUP=stations&FORMAT=JSON`, cached for
  two hours. The client waits 45 seconds. A timeout or `URLError` is HTTP
  504 with the reason as the body, then one retry. HTTP 301, 403, and 404
  are not retried. Records are validated, hashed, and stored once.
- A stations snapshot in `data/gp/`, written by
  `.github/workflows/celestrak-snapshot.yml` no faster than every two hours.
  **Load stations snapshot** opens that file and shows its retrieval time,
  age, and CelesTrak source. A failed live request does not load it.
- Per-quantity provenance: observation time, retrieval time, both ages,
  source, unit, frame, and measured / modelled / derived / unavailable.
- NOAA F10.7 in solar flux units and estimated Kp as a dimensionless index,
  only when those JSON files are retrieved. TEC and thermospheric density
  stay empty. Kp and F10.7 are not converted into density or plasma.
- Sampled geometric miss distance. Pc is not computed. Nc is not implemented.
  A missing covariance is not shown as zero or safe.
- A labelled simulation sample (`OG-SAMPLE-LEO-A` / `OG-SAMPLE-LEO-B`),
  shown at the element epoch, not as a live catalog.
- Space-Track is optional and server-side only. This build does not log in.
  If `SPACETRACK_USER` and `SPACETRACK_PASSWORD` are set, the status flag
  says they are configured and does not return the values.
- 3D ground-track display of latitudes and longitudes the API returns.
  The display frame is TEME rotated by GMST. It is not ITRF.

Operator notes, sources, and the list of things this version does not do
are in [`docs/orbitguard.md`](docs/orbitguard.md).

## Real-time tracking loop

`TrackingLoop` (`src/orbitguard/monitor/loop.py`) runs a continuous background
cycle: **retrieve -> store -> propagate (1/8 orbital period with NanoDynamics SGP4) -> geometric screen -> repeat**.

1. **Retrieve and observation-source plug-in point (`src/orbitguard/monitor/sensors.py`).**
   - **GitHub Actions CelesTrak GP snapshotter (`scripts/fetch_celestrak_snapshot.py` & `.github/workflows/celestrak-snapshot.yml`):**
     Hugging Face Space egress cannot reach `celestrak.org` directly (HTTP 504 / timeout), whereas GitHub Actions runners can reach CelesTrak and the Space can reach GitHub over HTTPS. Every 2 hours (`cron: "0 */2 * * *"`), the workflow runs `scripts/fetch_celestrak_snapshot.py` to pull the configured LEO and debris GP groups sequentially (`stations`, `active`, `cosmos-1408-debris`, `cosmos-2251-debris`, `fengyun-1c-debris`, `iridium-33-debris`), enforcing CelesTrak's 2-hour minimum interval independently per group via `data/gp/<group>.meta.json` and committing updated `data/gp/<group>.json` and `<group>.meta.json` files.
   - **GitHub snapshot runtime source (`GitHubSnapshotSource`, `"github_gp_snapshot"`):**
     When `ORBITGUARD_GITHUB_TOKEN` (private or public repo via GitHub REST contents API `https://api.github.com/repos/{owner}/{repo}/contents/data/gp/<group>.json?ref={branch}` with `Authorization: Bearer <token>`) or `ORBITGUARD_GITHUB_PUBLIC=1` (public repo via `https://raw.githubusercontent.com/{owner}/{repo}/{branch}/data/gp/<group>.json`) is configured, `TrackingLoop` uses `GitHubSnapshotSource` directly without attempting live CelesTrak calls that would hang on Hugging Face.
   - **Freshness honesty (`source_mode = "github_snapshot"`):**
     Data fetched via `GitHubSnapshotSource` is as fresh as the last successful 2-hour GitHub Actions snapshot job, never presented as a live CelesTrak session from the Space (`origin_flag = "snapshot"`, `live = False`, `snapshot = True`). Both `snapshot_age_s` and `data_age_s` are computed from each group's committed `<group>.meta.json` `retrieved_at` timestamp (when the Action downloaded from CelesTrak), not from the moment of the GitHub HTTP fetch. The GitHub token is never logged or exposed in status, provenance, or notes.
   - **Direct CelesTrak source (`CatalogElementSetSource`, `"celestrak_stations_gp"`) and bundled fallback (`source_mode = "snapshot_fallback"`):**
     When neither `ORBITGUARD_GITHUB_TOKEN` nor `ORBITGUARD_GITHUB_PUBLIC=1` is set, `TrackingLoop` uses `CatalogElementSetSource` (which attempts live CelesTrak GP OMM JSON downloads when `ORBITGUARD_LOOP_LIVE_FETCH=1`, or skips straight to the bundled `data/gp/stations.json` snapshot when `ORBITGUARD_LOOP_LIVE_FETCH=0` or when all group fetches fail). Without `ORBITGUARD_GITHUB_TOKEN` (private repo) or `ORBITGUARD_GITHUB_PUBLIC=1` (public repo), or if every GitHub group fetch fails, the loop falls back cleanly to the bundled `data/gp/stations.json` snapshot (`source_mode = "snapshot_fallback"`) with an explicit `fallback_reason`.
   - **Multi-group merge, LEO filter, and debris-preserving object cap (`ORBITGUARD_LOOP_MAX_OBJECTS`):**
     Records across all retrieved groups (`ORBITGUARD_LOOP_GROUPS`, default `stations,active,cosmos-1408-debris,cosmos-2251-debris,fengyun-1c-debris,iridium-33-debris`) are merged and de-duplicated by `NORAD_CAT_ID` (retaining the newest `EPOCH`), then filtered to LEO orbits (`perigee_altitude_km < 2000 km` computed from `MEAN_MOTION` and `ECCENTRICITY`). If the LEO object count exceeds `ORBITGUARD_LOOP_MAX_OBJECTS` (default `3000`; `0` means unlimited), objects are trimmed deterministically using a debris-preserving priority policy — keeping all `stations` first (tier 0), then all named debris groups (`cosmos-1408-debris`, `cosmos-2251-debris`, `fengyun-1c-debris`, `iridium-33-debris`, and any `*-debris` group, tier 1), then any other non-`active` groups (tier 2), and finally filling remaining slots from `active` (tier 3), sorted within each tier by `NORAD_CAT_ID` ascending so `active` never starves `stations` or the debris clouds — and reported honestly via `objects_capped`, `max_objects`, `objects_before_cap`, `objects_after_cap`, and `cap_reason`.
   - **Hardware observation-source stubs (`ObservationSource` ABC):**
     `LidarObservationSource` (`"lidar"`) and `VisionObservationSource`
     (`"xvision"`) report status `"not_connected"` and raise `NotConnected` on
     `fetch()`. Their docstrings specify the measurement record schemas
     (topocentric two-way slant range/angles for SLR/lidar; ICRF RA/Dec
     angles-only tracklets for `xvision`) and document that raw sensor
     measurements must pass through Initial Orbit Determination (IOD) and
     batch/sequential Orbit Determination (OD) before entering `TrackingLoop`.
2. **Store and SQLite memory (`<cache_dir>/tracking_loop.sqlite`).**
   - Uses Python stdlib `sqlite3` (WAL mode) with tables `iterations`,
     `element_sets`, `propagated_states`, `conjunction_screenings`, and
     `co_located_pairs`.
   - Retains the last `ORBITGUARD_LOOP_KEEP_ITERATIONS` iterations (default `10`)
     and prunes older rows automatically.
   - **Cross-iteration memory:** If an object's canonical OMM hash and `EPOCH`
     have not changed since the previous iteration and its cached propagation
     horizon still covers `now`, re-propagation is skipped and the cached
     TEME states are reused.
3. **Propagate (`1/8` orbit with NanoDynamics SGP4).**
   - For each tracked LEO object, orbital period is computed from mean motion
     as `1440 / MEAN_MOTION` minutes.
   - Each object is propagated forward with NanoDynamics SGP4 (`orbitguard.nd`)
     over a horizon of **`1/8` of its orbital period** at step
     `ORBITGUARD_LOOP_STEP_S` (default `30` s).
   - Loop cadence defaults to `1/8` of the shortest-period tracked object's
     orbital period, clamped to `[ORBITGUARD_LOOP_MIN_CADENCE_S, ORBITGUARD_LOOP_MAX_CADENCE_S]`
     (or overridden via `ORBITGUARD_LOOP_CADENCE_S`).
4. **Two-layer broad-phase pre-filter (`src/orbitguard/monitor/broadphase.py`) and exact geometric screen.**
   - **Layer 1 — Apogee/perigee (radial-overlap) sieve (`altitude_bands_overlap` / `count_radial_skipped_pairs`):**
     Eliminates pairs whose `[perigee - pad, apogee + pad]` radial bands (`pad = max(threshold_km, colocated_km)`) do not overlap in `O(n log n)` via sorted interval bounds (`radial_skipped_pair_count`).
   - **Layer 2 — 3D spatial hashing per time step with 27-neighborhood lookup (`screen_tracks_broadphase`):**
     At each shared propagation time step, every object's TEME position is binned into a uniform 3D grid with cell size `ORBITGUARD_LOOP_GRID_KM` (defaulting to `ORBITGUARD_LOOP_THRESHOLD_KM = 10.0` km and clamped up to `>= threshold_km` so a sub-threshold cell size can never miss a conjunction) using `cell_index = floor(position / cell_size)` along each axis. Only radial-surviving pairs that fall in the same cell or one of the 26 adjacent cells (`3x3x3` 27-neighborhood across faces, edges, and corners) at one or more time steps receive vectorized exact Euclidean miss-distance and relative-speed evaluation (`screened_pair_count`). Radial-surviving pairs that are never 27-neighbors at any step are eliminated without exact evaluation (`spatial_skipped_pair_count`).
   - **Pair count accounting:** `total_pair_count = n*(n-1)/2 = screened_pair_count + skipped_pair_count`, where `skipped_pair_count = radial_skipped_pair_count + spatial_skipped_pair_count`.
   - **Co-located (docked / attached) classification:** Pairs are classified under `co_located` if and only if they were present in the 27-neighborhood at **every** shared sample across the horizon (`neighbor_steps == shared_steps`) **and** remain within `ORBITGUARD_LOOP_COLOCATED_KM` (default `5.0` km) with relative speed `<= ORBITGUARD_LOOP_COLOCATED_REL_SPEED_KM_S` (default `0.01` km/s) across the entire horizon (if a pair is absent from the 27-neighborhood at any shared step, its separation at that step exceeds `cell_size >= threshold_km >= colocated_km`, so it cannot be co-located). Co-located pairs carry reason `"near-identical propagated states over the whole horizon; likely docked/attached or sharing an element set - not a close approach"`.
   - Remaining pairs with sampled miss distance `<= ORBITGUARD_LOOP_THRESHOLD_KM`
     (default `10.0` km) are flagged in `conjunctions` with label
     `"geometric screening - miss distance from propagated GP states, no collision probability, for human review"`.
     Collision probability (`Pc`) is never computed or output.

### Tracking loop environment variables

| Variable | Default | Purpose |
| --- | --- | --- |
| `ORBITGUARD_LOOP_ENABLED` | `1` | Start background tracking loop thread on FastAPI startup (`0` disables) |
| `ORBITGUARD_LOOP_GROUPS` | `stations,active,cosmos-1408-debris,cosmos-2251-debris,fengyun-1c-debris,iridium-33-debris` | Comma-separated CelesTrak GP groups to retrieve and merge by `NORAD_CAT_ID` |
| `ORBITGUARD_GITHUB_TOKEN` | unset | GitHub PAT / token for `GitHubSnapshotSource` via `api.github.com/repos/{owner}/{repo}/contents/...` (never logged or returned) |
| `ORBITGUARD_GITHUB_PUBLIC` | `0` | Set to `1` when no token is set to fetch committed snapshots via `raw.githubusercontent.com` |
| `ORBITGUARD_GITHUB_OWNER` | `joachimgrobbelaar` | GitHub repository owner for `GitHubSnapshotSource` |
| `ORBITGUARD_GITHUB_REPO` | `orbitguard-ai` | GitHub repository name for `GitHubSnapshotSource` |
| `ORBITGUARD_GITHUB_BRANCH` | `master` | GitHub branch/ref where scheduled snapshots are committed (can point at another branch) |
| `ORBITGUARD_LOOP_MAX_OBJECTS` | `3000` | Maximum tracked LEO objects after dedup and LEO filter (`0` disables cap; deterministic priority keeps all `stations` and named `*-debris` groups before filling remaining slots from `active` by `NORAD_CAT_ID`) |
| `ORBITGUARD_LOOP_LIVE_FETCH` | `1` | When GitHub source is not configured, attempt live CelesTrak GP fetch before falling back to bundled `stations.json` (`0` forces bundled snapshot fallback) |
| `ORBITGUARD_LOOP_FETCH_TIMEOUT_S` | `8.0` | Per-group HTTP timeout (seconds) before skipping/falling back |
| `ORBITGUARD_LOOP_STEP_S` | `30.0` | SGP4 propagation sample step in seconds |
| `ORBITGUARD_LOOP_THRESHOLD_KM` | `10.0` | Geometric close-approach miss-distance threshold in km |
| `ORBITGUARD_LOOP_GRID_KM` | `ORBITGUARD_LOOP_THRESHOLD_KM` (`10.0`) | 3D spatial-hash cell size in km for the 27-neighborhood broad-phase (clamped up to `>= ORBITGUARD_LOOP_THRESHOLD_KM` if set smaller) |
| `ORBITGUARD_LOOP_COLOCATED_KM` | `5.0` | Maximum separation (km) across horizon to classify a pair as `co_located` |
| `ORBITGUARD_LOOP_COLOCATED_REL_SPEED_KM_S` | `0.01` | Maximum relative speed (km/s) across horizon to classify a pair as `co_located` |
| `ORBITGUARD_LOOP_CADENCE_S` | unset (`shortest_period / 8`) | Optional explicit loop cadence override in seconds |
| `ORBITGUARD_LOOP_MIN_CADENCE_S` | `10.0` | Minimum loop cadence bound in seconds |
| `ORBITGUARD_LOOP_MAX_CADENCE_S` | `3600.0` | Maximum loop cadence bound in seconds |
| `ORBITGUARD_LOOP_KEEP_ITERATIONS` | `10` | Number of recent iterations retained in SQLite |

### Tracking loop HTTP endpoints

- `GET /api/orbitguard/loop/status` — loop state, iteration counts, active `data_source` (`GitHubSnapshotSource` vs `CatalogElementSetSource` and origin `github` vs `celestrak` vs `bundled_fallback`), `source_mode` (`"github_snapshot"`, `"live"`, or `"snapshot_fallback"`), `object_count`, `groups_requested` vs `groups_retrieved`, cap reporting (`objects_capped`, `max_objects`, `objects_before_cap`, `objects_after_cap`, `cap_reason`), `fallback_reason`, `snapshot_age_s`, `data_age_s`, broad-phase pair accounting (`total_pair_count`, `screened_pair_count`, `skipped_pair_count`, `radial_skipped_pair_count`, `spatial_skipped_pair_count`, `grid_km`), observation-source statuses, and SQLite row counts.
- `GET /api/orbitguard/loop/conjunctions` — latest geometric close-approach screenings (`conjunctions`) and separated docked/attached assemblies (`co_located`) with provenance.
- `GET /api/orbitguard/loop/objects` — latest tracked LEO objects with `epoch`, `epoch_age_hours`, perigee/apogee altitudes, `1/8`-period propagated horizon, and provenance.
- `POST /api/orbitguard/loop/run-once` — trigger one synchronous tracking loop iteration immediately.

## Install on your phone (PWA)

OrbitGuard ships as an installable Progressive Web App (PWA):

- **Web App Manifest (`GET /manifest.webmanifest`):** Declares `name: "OrbitGuard"`, `short_name: "OrbitGuard"`, `start_url: "/?source=pwa"`, `scope: "/"`, `display: "standalone"` (`display_override: ["fullscreen", "standalone"]`), `orientation: "any"`, theme/background `#030712`, and icons (`192x192`, `512x512`, `512x512` maskable PNG, `180x180` `apple-touch-icon.png`, and `icon.svg`, generated deterministically by `scripts/generate_pwa_icons.py`).
- **Install on Android / desktop Chrome & Edge:** Tap the **Install app** button in the header (powered by `beforeinstallprompt`) or choose **Install app / Add to Home Screen** from the browser menu.
- **Install on iOS Safari:** Open OrbitGuard in Safari and tap **Share -> Add to Home Screen** (an on-screen hint is shown on iOS Safari when not already running standalone).
- **Standalone full-screen live globe mode:** When launched from the home screen (`display-mode: standalone` / `fullscreen` or `/?source=pwa`), the 3D Earth globe fills the screen, telemetry and screening panels collapse into a **Panels** overlay toggle, and a compact always-visible status chip displays the loop source mode (`LIVE` vs `SNAPSHOT FALLBACK`) and data age reported by the API.
- **Service worker (`GET /sw.js`) and offline honesty:** Precaches the static app shell (`/`, manifest, icons, and opportunistically Three.js CDN scripts) so the UI shell loads quickly, using network-first for `/` and cache-first for static assets. **It never caches or serves cached `/api/` responses.** When the device is offline or API calls fail, `/api/` requests return HTTP `503` JSON `{"error":"offline","detail":"No network; OrbitGuard does not show cached telemetry as live."}` and the UI displays an offline banner stating that telemetry is not live.

> **Note on native OS home-screen widgets:** A true native OS home-screen widget (iOS WidgetKit or Android App Widget) is not possible from a web app or Hugging Face Space; building an OS widget would require a small companion native iOS/Android app calling the OrbitGuard HTTP API (`/api/orbitguard/loop/status` and `/api/orbitguard/loop/conjunctions`). Installing the OrbitGuard PWA (**Add to Home Screen**, which launches full-screen directly to the live globe) is the realistic web-delivered path.

## What is not implemented

No validated 2D Pc, 3D Nc, or Monte Carlo count. No covariance propagation.
No NRLMSISE-00 or JB2008. No GNSS TEC, in-situ plasma, ILRS, or debris-laser
feed. No laser pointing or firing. No maneuver solver and no command path.
No invented telemetry.

## Run locally

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
pytest -v
python -m uvicorn orbitguard.main:app --host 0.0.0.0 --port 7860
```

`pip install -r requirements.txt` installs NanoDynamics from the pinned
git URL. Catalog ingest, SGP4 tracks, and the NOAA request need no
credentials.

The catalog cache defaults to `/tmp/orbitguard` (`ORBITGUARD_CACHE_DIR`).
No credentials are required.

## Docker

```bash
docker build -t orbitguard .
docker run --rm -p 7860:7860 orbitguard
```

The image follows the Hugging Face Docker Space rules: non-root user
`uid 1000`, cache under `/tmp/orbitguard`, process listening on
`0.0.0.0:7860`. The same `requirements.txt` pin is installed in the image.
No secrets are baked in.

## Hugging Face Space

The README front matter above is the Space config (`sdk: docker`,
`app_port: 7860`). `.github/workflows/deploy-huggingface.yml` pushes `main`
to the Space with the `HF_TOKEN` repository secret. The Space id is the
single non-comment line in [`deploy/hf_space_id`](deploy/hf_space_id):
`Marnu25/orbitguard-ai`. A NanoDynamics workflow used to force-push that
Space; it no longer does. This workflow is the publisher.

The upload script is `scripts/publish_space.py`. It does not print the token.

`NANODYNAMICS_URL` defaults to `https://marnu25-nanodynamics.hf.space`.
Set that environment variable to point the header link somewhere else.
The same default is the non-comment line in
[`deploy/nanodynamics_space_url`](deploy/nanodynamics_space_url).
`GET /api/orbitguard/links` returns the address the page uses. The HTML
does not hardcode it.

This Space is the existing private Docker Space `Marnu25/orbitguard-ai`.
The image build does not read `HF_TOKEN` or any other secret. Pushing
`main` runs `.github/workflows/deploy-huggingface.yml`, which uploads
this repository root with the `HF_TOKEN` secret. No token was available
in this workspace; deploy the commit named in the pull request.
