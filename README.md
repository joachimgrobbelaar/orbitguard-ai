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
  two hours. HTTP 301, 403, and 404 are not retried. Records are validated,
  hashed, and stored once.
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
