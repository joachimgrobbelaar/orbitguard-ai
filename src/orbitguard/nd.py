"""Pinned NanoDynamics 1.1.0 dependency.

OrbitGuard imports the public modules below. It does not reimplement them.

The requirement line, in ``requirements.txt`` as well, is::

    nanodynamics @ git+https://github.com/joachimgrobbelaar/NanoDynamics@d22c000a304e4484ce58461afde31981647483bf

``nanodynamics.elements``
    ``validate_omm``, ``canonical_hash``, ``encode_alpha5``, ``decode_alpha5``,
    ``checksum``, ``checksum_matches``, ``catalog_number_from_line1``,
    ``epoch_from_line1`` (plus ``parse_epoch`` and ``RecordRejected`` from
    the same module, used when storing a validated record).

``nanodynamics.frames``
    ``gmst_deg``, ``julian_date``, ``rotation_teme_to_display``,
    ``teme_to_display``, ``ensure_utc``.

``nanodynamics.propagation``
    ``satrec_from_omm``, ``satrec_from_tle``, ``propagate_sgp4``,
    ``propagate_osculating_cartesian`` (rejects GP/TLE/TEME),
    ``two_body_max_position_error_m``.

``nanodynamics.constants``
    Frame names, the WGS-72 label, and GMST at J2000.
"""

from __future__ import annotations

from typing import Any

from nanodynamics import __version__ as NANODYNAMICS_VERSION
from nanodynamics.constants import (
    CELESTRAK_GP_DOC,
    DISPLAY_FRAME,
    DISPLAY_FRAME_NOTE,
    GMST_J2000_DEG,
    GP_CENTER,
    GP_MEAN_ELEMENT_THEORY,
    GP_REFERENCE_FRAME,
    GP_TIME_SYSTEM,
    NUMERICAL_FRAME,
    NUMERICAL_FRAME_NOTE,
    SGP4_GRAVITY,
)
from nanodynamics.elements import (
    RecordRejected,
    canonical_hash,
    catalog_number_from_line1,
    checksum,
    checksum_matches,
    decode_alpha5,
    encode_alpha5,
    epoch_from_line1,
    parse_epoch,
    validate_omm,
)
from nanodynamics.frames import (
    ensure_utc,
    gmst_deg,
    julian_date,
    rotation_teme_to_display,
    teme_to_display,
)
from nanodynamics.propagation import (
    MU_EARTH,
    MeanElementsNotOsculating,
    R_EARTH,
    propagate_osculating_cartesian,
    propagate_sgp4,
    satrec_from_omm,
    satrec_from_tle,
    two_body_max_position_error_m,
)

# Standard WGS-72 constants used by SGP4 alongside NanoDynamics Earth constants.
WGS72_MU_KM3_S2 = 398600.8
WGS72_RADIUS_EARTH_KM = 6378.135
EARTH_MU_KM3_S2 = float(MU_EARTH) * 1e-9 if MU_EARTH else WGS72_MU_KM3_S2
EARTH_RADIUS_KM = float(R_EARTH) * 1e-3 if R_EARTH else WGS72_RADIUS_EARTH_KM

PIN_SHA = "d22c000a304e4484ce58461afde31981647483bf"
PIN_REPO = "https://github.com/joachimgrobbelaar/NanoDynamics"
REQUIREMENT = f"nanodynamics @ git+{PIN_REPO}@{PIN_SHA}"

__all__ = [
    "CELESTRAK_GP_DOC",
    "DISPLAY_FRAME",
    "DISPLAY_FRAME_NOTE",
    "EARTH_MU_KM3_S2",
    "EARTH_RADIUS_KM",
    "GMST_J2000_DEG",
    "GP_CENTER",
    "GP_MEAN_ELEMENT_THEORY",
    "GP_REFERENCE_FRAME",
    "GP_TIME_SYSTEM",
    "MU_EARTH",
    "MeanElementsNotOsculating",
    "NUMERICAL_FRAME",
    "NUMERICAL_FRAME_NOTE",
    "PIN_SHA",
    "REQUIREMENT",
    "R_EARTH",
    "RecordRejected",
    "SGP4_GRAVITY",
    "WGS72_MU_KM3_S2",
    "WGS72_RADIUS_EARTH_KM",
    "canonical_hash",
    "catalog_number_from_line1",
    "checksum",
    "checksum_matches",
    "decode_alpha5",
    "encode_alpha5",
    "ensure_utc",
    "epoch_from_line1",
    "gmst_deg",
    "julian_date",
    "parse_epoch",
    "propagate_osculating_cartesian",
    "propagate_sgp4",
    "rotation_teme_to_display",
    "satrec_from_omm",
    "satrec_from_tle",
    "teme_to_display",
    "two_body_max_position_error_m",
    "validate_omm",
]


def api_status() -> dict[str, Any]:
    return {
        "available": True,
        "distribution": "nanodynamics",
        "version": NANODYNAMICS_VERSION,
        "requirement": REQUIREMENT,
        "pin": PIN_SHA,
        "expected_modules": [
            "nanodynamics.elements",
            "nanodynamics.frames",
            "nanodynamics.propagation",
            "nanodynamics.constants",
        ],
        "detail": f"nanodynamics {NANODYNAMICS_VERSION} at {PIN_SHA}",
    }
