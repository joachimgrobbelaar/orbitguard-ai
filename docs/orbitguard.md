# OrbitGuard operator notes

OrbitGuard is a standalone LEO monitoring product. It depends on the
NanoDynamics package for SGP4, the GMST display transform, element-field
parsing, physical constants, and the numerical propagator. NanoDynamics
does not import OrbitGuard. This page shows what was retrieved, what was
derived, and what is not known. It does not command a spacecraft.

## What is running

1. **Ingest.** One public CelesTrak request for the `stations` group, as
   OMM JSON:
   `https://celestrak.org/NORAD/elements/gp.php?GROUP=stations&FORMAT=JSON`.
   The client caches the batch and does not ask again for two hours,
   which is the update interval CelesTrak documents. The read waits 45
   seconds. A timeout or other URL error is reported as HTTP 504 with
   the reason as the body, retried once, and is not turned into an empty
   catalog. HTTP 301, 403, and 404 are not retried. Records are validated,
   hashed, and stored once. A separate GitHub Actions job saves one
   stations-group response at least two hours apart under `data/gp/`.
   Opening that file is its own action. The page names it as a snapshot,
   with the job's retrieval time and the CelesTrak URL. A failed live
   request does not load the file by itself.
   The same hash is a duplicate. The same catalog number and epoch with
   a different hash is a conflict and is not overwritten. Fields that
   look like credentials are rejected. Legacy TLE lines can be checked
   for a checksum and for a strict Alpha-5 catalog field (`I` and `O`
   are rejected; the ceiling is `Z9999` = 339999).
2. **Propagation of catalog elements.** Mean elements go through SGP4
   with the WGS-72 constants and the improved operation mode. Output is
   TEME. They are not handed to the numerical integrator.
3. **Display transform.** TEME is rotated by Greenwich mean sidereal
   time into an Earth-fixed display frame, then converted to WGS-84
   geodetic coordinates. UTC is used in place of UT1. Polar motion and
   the equation of equinoxes are not applied, so the frame is not ITRF.
4. **Numerical simulation.** NanoDynamics' numerical propagator runs only
   when the caller labels the state `osculating_cartesian` in the
   `nanodynamics_eci` frame (the package constant `NUMERICAL_FRAME`).
   A regression test compares a drag-free, J2-free circular orbit with
   the closed-form Keplerian position. A separate check compares the sign
   of nodal drift with the first-order J2 secular rate. Osculating
   elements are not that secular rate. Neither check is a PINN accuracy
   claim. The integrator is not copied into this repository.
5. **SGP4 wiring check.** The epoch state of the standard verification
   element (catalog 00005 in the python-sgp4 `tcppver.out` vector) matches
   that published TEME coordinate. That element is not a current object
   and the match is not an operational error budget.
6. **Environment.** When the download succeeds, F10.7 comes from
   `https://services.swpc.noaa.gov/json/f107_cm_flux.json` (solar flux
   units, 2800 MHz) and planetary K from
   `https://services.swpc.noaa.gov/json/planetary_k_index_1m.json`
   (dimensionless). On 2026-11-06,
   `https://services.swpc.noaa.gov/json/planetary_k_index.json` returned
   HTTP 404, so OrbitGuard does not use it. TEC and thermospheric density
   are left empty.
7. **Geometry.** A screen reports the closest sampled separation in one
   frame. It does not report a collision probability.

## What is not running

- No 2D Pc, no Hall 3D Nc, and no Monte Carlo collision count.
- Nc, as defined by Hall (Journal of Spacecraft and Rockets, 2021,
  https://doi.org/10.2514/1.A34919), is an expected number of collisions.
  The paper states that Nc equals Pc for an isolated encounter under its
  assumptions, and that Nc can exceed Pc when encounters repeat. OrbitGuard
  does not compute Nc and does not relabel it as Pc. 3D Nc is not a
  fallback for a missing 2D input, and it does not repair a bad covariance.
  The NASA software record is https://software.nasa.gov/software/GSC-18593-1.
  The CARA briefing is
  https://ntrs.nasa.gov/api/citations/20205007226/downloads/CARA_3D-NC%20and%20BFMC%20Presentation_ATrainMOWG_Fall2020_Final.pdf.
  The conjunction-assessment handbook appendix distributed by NTRS is
  https://ntrs.nasa.gov/api/citations/20240003468/downloads/CA_Hanbook_Appendix.pdf.
- No covariance propagation. A supplied matrix is checked for shape,
  finite values, symmetry, positive-semidefiniteness, epoch, and frame.
  There is no state-transition matrix and no process noise. Public GP
  has no calibrated covariance. Absence is not shown as zero or safe.
- No uncertainty ellipsoid or Gaussian splat. A drawing would not be a
  probability.
- NRLMSISE-00 (https://ccmc.gsfc.nasa.gov/models/NRLMSIS~00/) and JB2008
  (https://ccmc.gsfc.nasa.gov/models/JB2008~2008/) are not evaluated.
- No GNSS TEC product and no in-situ plasma product. TEC is an integrated
  column (TECU = 10^16 electrons m^-2), not a local density. Kp and F10.7
  are not turned into either quantity.
- No optical, SLR, or lidar feed. Optical data would be lines of sight
  and brightness, not ranges. Cooperative satellite laser ranging and
  uncooperative debris ranging are different, and both need an external
  network (see https://ilrs.gsfc.nasa.gov/lw19/docs/2014/Papers/3009_Kirchner_paper.pdf).
  Debris-ranging distance and accuracy depend on the station and the
  target; none is stated here. A conventional lidar does not map LEO.
  Transmitting a laser would require the aviation and laser-safety
  approvals that apply at the site, and a partner who can perform the
  ranging. This software does not treat a U.S. Laser Clearinghouse
  process as the rule for work in South Africa, and it does not point
  or fire a laser.
- Space-Track (https://www.space-track.org/documentation) is not called.
  If `SPACETRACK_USER` and `SPACETRACK_PASSWORD` exist in the server
  environment, the status flag says only that they are configured. The
  values are not returned. This preview has no account and no login.
- No maneuver solver and no command path.

## Ages

Element epoch age and retrieval age are different. Epoch age is the age
of the mean elements. Retrieval age is the time since this process last
stored a successful download. The default labels use 72 hours for the
epoch and 6 hours for the retrieval. Both numbers are display settings
for this workspace. CelesTrak's own "oldest" table flag (3.5 days for
active satellites) is a data-review aid, not a safety rule, and OrbitGuard
does not adopt it as one. A state inside the display window is not a
statement that the orbit is safe.

## Sample mode

The simulation sample is two synthetic element sets named
`OG-SAMPLE-LEO-A` and `OG-SAMPLE-LEO-B`. Their integers exist only so
SGP4 can start. They are not NORAD identities. States are shown at the
element epoch (2024-06-01), not slid forward to today and labelled live.
The banner and every object row say simulation sample.

## Frames and constants

| Quantity | Value used here |
| --- | --- |
| Catalog theory | SGP4, WGS-72 constants, TEME, UTC |
| Display | TEME rotated by GMST; WGS-84 geodetic |
| Numerical frame | `nanodynamics_eci` (J2 along the NanoDynamics model's z axis) |
| GMST at 2000-01-01 12:00 UTC | 280.46061837 degrees |

GP JSON omits `CENTER_NAME`, `REF_FRAME`, `TIME_SYSTEM`, and
`MEAN_ELEMENT_THEORY`. CelesTrak's formats page states the omitted
values as EARTH, TEME, UTC, and SGP4:
https://celestrak.org/NORAD/documentation/gp-data-formats.php.

## Interfaces

Passive ingest, normalization, environment, geometry, and the page are
modules under `src/orbitguard/monitor/`. SGP4, frames, element parsing,
and numerical propagation are called through `orbitguard.nd`, which
imports `nanodynamics.propagation`, `nanodynamics.frames`,
`nanodynamics.elements`, and `nanodynamics.constants`. The HTTP surface
is `/api/orbitguard/`. The earlier copilot that claimed autonomous
maneuvers is not part of this product and is not a validated probability
service.

## Real-time tracking loop and observation sources

`TrackingLoop` (`src/orbitguard/monitor/loop.py`) runs the continuous
cycle `retrieve -> store -> propagate -> screen -> repeat`:

1. **Retrieve (`ObservationSource` plug-in interface in `monitor/sensors.py`).**
   - **GitHub Actions CelesTrak GP snapshotter (`scripts/fetch_celestrak_snapshot.py` and `.github/workflows/celestrak-snapshot.yml`):**
     Because Hugging Face Space egress cannot reach `celestrak.org` (HTTP 504 / timeout), a scheduled GitHub Actions workflow runs every 2 hours (`0 */2 * * *`) to pull the configured CelesTrak GP groups sequentially:
     `stations`, `active`, `cosmos-1408-debris`, `cosmos-2251-debris`, `fengyun-1c-debris`, `iridium-33-debris`.
     It enforces CelesTrak's 2-hour minimum refetch rule independently per group via `data/gp/<group>.meta.json` and commits `data/gp/<group>.json` and `<group>.meta.json` to the repository.
   - **Runtime GitHub snapshot source (`GitHubSnapshotSource`, `"github_gp_snapshot"`):**
     When `ORBITGUARD_GITHUB_TOKEN` (uses `https://api.github.com/repos/{owner}/{repo}/contents/data/gp/<group>.json?ref={branch}` with `Authorization: Bearer <token>`, decoding base64 `content`) or `ORBITGUARD_GITHUB_PUBLIC=1` (uses `https://raw.githubusercontent.com/{owner}/{repo}/{branch}/data/gp/<group>.json` without auth) is set, `TrackingLoop` selects `GitHubSnapshotSource` and does not attempt live CelesTrak calls. Defaults are `ORBITGUARD_GITHUB_OWNER=joachimgrobbelaar`, `ORBITGUARD_GITHUB_REPO=orbitguard-ai`, `ORBITGUARD_GITHUB_BRANCH=master` (`ORBITGUARD_GITHUB_BRANCH` can point at another branch). The token is never logged or returned in status, provenance, or notes.
   - **Freshness honesty (`source_mode = "github_snapshot"`):**
     Data retrieved through `GitHubSnapshotSource` is as fresh as the last successful 2-hour GitHub Actions snapshot job — never presented as a live CelesTrak download (`live = False`, `snapshot = True`, `origin_flag = "snapshot"`). `snapshot_age_s` and `data_age_s` are computed from each group's committed `<group>.meta.json` `retrieved_at` timestamp, not from the runtime GitHub HTTP request time.
   - **Direct CelesTrak source (`CatalogElementSetSource`) and bundled fallback (`source_mode = "snapshot_fallback"`):**
     Without `ORBITGUARD_GITHUB_TOKEN` (private repo) or `ORBITGUARD_GITHUB_PUBLIC=1` (public repo), `TrackingLoop` uses `CatalogElementSetSource` and falls back to the bundled `data/gp/stations.json` snapshot when `ORBITGUARD_LOOP_LIVE_FETCH=0` or when live CelesTrak egress fails (`source_mode = "snapshot_fallback"`). Likewise, if `GitHubSnapshotSource` is unconfigured or if every requested group fails on GitHub, it falls back cleanly to the bundled `stations.json` snapshot with an explicit `fallback_reason`.
   - **Multi-group merge, LEO filter, and debris-preserving max-objects cap (`ORBITGUARD_LOOP_MAX_OBJECTS`):**
     Configured groups (`ORBITGUARD_LOOP_GROUPS`, default `stations,active,cosmos-1408-debris,cosmos-2251-debris,fengyun-1c-debris,iridium-33-debris`) are merged and de-duplicated by `NORAD_CAT_ID` (retaining the newest `EPOCH`), then filtered to LEO orbits (`perigee_altitude_km < 2000 km`). If the LEO object count exceeds `ORBITGUARD_LOOP_MAX_OBJECTS` (default `3000`; `0` means unlimited), the loop trims deterministically using a debris-preserving priority policy — keeping all `stations` objects first (tier 0), then all named debris groups (`cosmos-1408-debris`, `cosmos-2251-debris`, `fengyun-1c-debris`, `iridium-33-debris`, and any `*-debris` group, tier 1), then any other non-`active` groups (tier 2), and finally filling remaining slots from `active` (tier 3) sorted by `NORAD_CAT_ID` ascending so `active` never starves `stations` or the debris clouds — and reports `objects_capped`, `max_objects`, `objects_before_cap`, `objects_after_cap`, and `cap_reason` in `/api/orbitguard/loop/status`.
   - **Hardware sensor stubs:** `LidarObservationSource` (`"lidar"`) and
     `VisionObservationSource` (`"xvision"`) implement `ObservationSource`
     with status `"not_connected"` and raise `NotConnected` on `fetch()`,
     documenting how future range/angle or angles-only astrometric tracklets
     would have to pass through initial and batch/sequential orbit
     determination (OD) before entering the loop. They never emit data.
2. **Propagate.** Each tracked object is propagated with NanoDynamics SGP4
   (`orbitguard.nd`) over a horizon of `1/8` of its orbital period
   (`1440 / MEAN_MOTION` minutes divided by 8) at a configurable step
   (`ORBITGUARD_LOOP_STEP_S`, default 30 s). The loop cadence is the horizon
   length of the shortest-period tracked object (`shortest_period / 8`),
   clamped to `[ORBITGUARD_LOOP_MIN_CADENCE_S, ORBITGUARD_LOOP_MAX_CADENCE_S]`
   (or overridden via `ORBITGUARD_LOOP_CADENCE_S`). When an object's canonical
   element-set hash and epoch are unchanged and its previously propagated
   horizon still covers `now`, re-propagation is skipped.
3. **Two-layer broad-phase pre-filter (`src/orbitguard/monitor/broadphase.py`) and exact geometric screen.**
   - **Layer 1 — Apogee/perigee (radial-overlap) sieve (`altitude_bands_overlap` / `count_radial_skipped_pairs`):**
     Eliminates pairs whose `[perigee - pad, apogee + pad]` altitude bands (`pad = max(threshold_km, colocated_km)`) do not overlap in `O(n log n)` via sorted interval bounds (`radial_skipped_pair_count`).
   - **Layer 2 — 3D spatial hashing per time step with 27-neighborhood lookup (`screen_tracks_broadphase`):**
     At each shared propagation time step, bins every object's TEME position into a uniform 3D grid with cell size `ORBITGUARD_LOOP_GRID_KM` (defaulting to `ORBITGUARD_LOOP_THRESHOLD_KM = 10.0` km, and clamped up to `>= ORBITGUARD_LOOP_THRESHOLD_KM` if configured smaller) using `cell_index = floor(position / cell_size)` along each axis. Only radial-surviving pairs that fall in the same cell or one of the 26 adjacent cells (`3x3x3` 27-neighborhood across faces, edges, and corners) at one or more time steps receive vectorized exact Euclidean miss-distance and relative-speed evaluation (`screened_pair_count`). Radial-surviving pairs that are never 27-neighbors at any step are eliminated (`spatial_skipped_pair_count`).
   - **Pair count accounting:** `/api/orbitguard/loop/status` and `/api/orbitguard/loop/conjunctions` report `total_pair_count = n*(n-1)/2 = screened_pair_count + skipped_pair_count`, where `skipped_pair_count = radial_skipped_pair_count + spatial_skipped_pair_count`, along with `grid_km`.
   - **Co-located (docked / attached) classification:** A pair is classified under `co_located` if and only if it was present in the 27-neighborhood at **every** shared sample across the horizon (`neighbor_steps == shared_steps`) **and** its measured `max_miss_km <= ORBITGUARD_LOOP_COLOCATED_KM` (default 5 km) and `max_relative_speed_km_s <= ORBITGUARD_LOOP_COLOCATED_REL_SPEED_KM_S` (default 0.01 km/s). If a pair is missing from the 27-neighborhood at any shared step, its separation at that step exceeds `cell_size >= threshold_km >= colocated_km`, so it cannot be co-located.
   - Remaining pairs within `ORBITGUARD_LOOP_THRESHOLD_KM`
     (default 10 km) are flagged for human review with label
     `"geometric screening - miss distance from propagated GP states, no collision probability, for human review"`.
     No collision probability (`Pc`) is ever computed or output.
4. **Persist.** Iterations, element sets, propagated states, co-located pairs, and
   conjunction screenings are stored in `<cache_dir>/tracking_loop.sqlite`
   (stdlib `sqlite3`), pruning rows beyond the last `ORBITGUARD_LOOP_KEEP_ITERATIONS`
   (default 10) iterations. The loop starts on FastAPI startup unless
   `ORBITGUARD_LOOP_ENABLED=0` and exposes `GET /api/orbitguard/loop/status`,
   `GET /api/orbitguard/loop/conjunctions`,
   `GET /api/orbitguard/loop/objects`, and
   `POST /api/orbitguard/loop/run-once`.

## Install on your phone (PWA)

OrbitGuard can be installed on a phone or tablet home screen as a
Progressive Web App (PWA):

- `GET /manifest.webmanifest` (`application/manifest+json`), `GET /sw.js`
  (`application/javascript`, `Service-Worker-Allowed: /`,
  `Cache-Control: no-cache`), `GET /icons/{name}`, and
  `GET /apple-touch-icon.png` serve the PWA manifest, service worker, and
  globe/orbit icons (`192x192`, `512x512`, `512x512` maskable PNG,
  `180x180` Apple touch icon, and `icon.svg`, generated via Python stdlib
  `zlib`/`struct` in `scripts/generate_pwa_icons.py`).
- **Android / Chrome / Edge:** Use the **Install app** button
  (`beforeinstallprompt`) or the browser menu's **Add to Home Screen**.
- **iOS Safari:** Tap **Share -> Add to Home Screen** (shown as a hint
  only on iOS Safari when not already running standalone).
- **Standalone full-screen live globe mode:** Launching from the home
  screen (`/?source=pwa` or `display-mode: standalone` / `fullscreen`)
  fills the screen with the live 3D globe, collapses the detail and
  tracking-loop panels behind a **Panels** overlay toggle, and keeps a
  compact status chip visible showing loop source mode (`LIVE` vs
  `SNAPSHOT FALLBACK`) and data age from the API only.
- **Offline honesty:** The service worker caches only the static app shell
  and never caches `/api/` telemetry. If the network is unavailable, `/api/`
  requests return HTTP 503 JSON
  `{"error":"offline","detail":"No network; OrbitGuard does not show cached telemetry as live."}`
  and an offline banner states that data is not live.
- **Native widget limitation:** A true native OS home-screen widget
  (iOS WidgetKit / Android App Widget) is not possible from a web app or
  Hugging Face Space; it would require a small companion native app
  calling the OrbitGuard API. The PWA (**Add to Home Screen**, launching
  full-screen to the live globe) is the realistic path.

## Dependency direction

OrbitGuard installs NanoDynamics 1.1.0. NanoDynamics does not import
OrbitGuard. The pin is:

`nanodynamics @ git+https://github.com/joachimgrobbelaar/NanoDynamics@d22c000a304e4484ce58461afde31981647483bf`

Element validation, SGP4, the GMST display transform, frame names, and
the numerical propagator are that package's modules. This repository
does not keep a second copy. The header link defaults to
`https://marnu25-nanodynamics.hf.space` unless `NANODYNAMICS_URL` is set.
The page reads that address from `GET /api/orbitguard/links`.
