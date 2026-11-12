"""What this OrbitGuard version does, and what it deliberately leaves undone."""

from __future__ import annotations

from typing import Any


def roadmap() -> dict[str, Any]:
    return {
        "component": "OrbitGuard",
        "version": "0.1.0",
        "role": (
            "Standalone LEO monitoring product. Orbital propagation, frames, and "
            "element parsing are imported from the NanoDynamics package. "
            "NanoDynamics does not depend on OrbitGuard."
        ),
        "implemented": [
            "This repository is the OrbitGuard product. Shared SGP4, frame, element, and numerical-propagator code is imported from NanoDynamics and is not copied here.",
            "Public CelesTrak GP/OMM JSON for the stations group, one cached batch, FORMAT=JSON. Legacy TLE checksums and strict Alpha-5 decoding are provided by NanoDynamics.",
            "Idempotent ingest: content hash, duplicate detection, conflict when the same catalog id and epoch arrive with a different hash. Credential-like fields are rejected.",
            "Catalog mean elements are propagated by NanoDynamics SGP4 (WGS-72, TEME) and shown with that package's GMST Earth-fixed display transform. UTC stands in for UT1.",
            "Numerical propagation is NanoDynamics' integrator, and only for states labelled osculating Cartesian in its numerical frame. Mean elements are refused.",
            "Provenance on each quantity: observation time, retrieval time, both ages, source, unit, frame, and measured/modelled/derived/unavailable.",
            "Epoch age and retrieval age are separate. Stale labels use configurable display settings.",
            "NOAA F10.7 and estimated planetary K when those JSON files are retrieved. TEC and thermospheric density stay unavailable.",
            "Sensor page states optical, cooperative SLR, debris laser ranging, conventional lidar, TEC, and in-situ plasma limits. No feed is connected.",
            "Sampled geometric miss distance on a common time grid. Pc is not computable. Nc is not implemented.",
            "Covariance gate: absent, non-finite, asymmetric, non-PSD, or frame/epoch mismatch never becomes 0 or safe.",
            "Labelled simulation sample with synthetic objects, shown at the element epoch.",
        ],
        "not_implemented": [
            "No calibrated 2D Pc, no Hall 3D Nc, and no Monte Carlo collision count.",
            "No covariance propagation (no state transition matrix, no process noise).",
            "No NRLMSISE-00 or JB2008 density evaluation.",
            "No GNSS TEC map and no in-situ plasma ingest.",
            "No ILRS or debris-laser ingest, and no laser pointing or firing.",
            "No Space-Track login. Credentials would stay server-side; this preview does not call the service.",
            "No IERS Earth-orientation bulletin, so the display frame is not ITRF.",
            "No mixing of a SGP4/TEME state into the numerical integrator.",
            "No autonomous maneuver, no avoidance burn, and no spacecraft command.",
            "No claim that a drawn ellipsoid or Gaussian splat is a collision probability.",
            "PINN surrogates are not used and are not treated as validated ephemerides.",
        ],
        "dependencies": [
            "CelesTrak public GP for the stations group. Larger groups are not downloaded.",
            "NOAA SWPC JSON for F10.7 and planetary K. Either file can be down without inventing the other.",
            "A qualified covariance, hard-body radius, and a checked Pc or Nc implementation before any probability is shown.",
            "An external laser ranging partner, plus local aviation and laser-safety approval, before any ranging claim.",
            "Space-Track account only if a later server-side adapter is turned on. Not required for this preview.",
        ],
    }
