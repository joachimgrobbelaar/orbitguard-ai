"""Product settings for OrbitGuard.

Frame names, the WGS-72 label, and the GMST constant live in
``nanodynamics.constants``. Thresholds below are operator display settings.
They are not 18 SDS, NASA CARA, or collision-safety criteria.
"""

from __future__ import annotations

# Product display settings, not orbital safety rules.
# CelesTrak states that it checks for new GP data once every 2 hours and
# asks clients not to download a group more often than that.
# https://celestrak.org/NORAD/documentation/gp-data-formats.php
CELESTRAK_MIN_REFETCH_S = 2 * 3600
# How old an element epoch may be before the workspace marks it
# "older than the display setting". CelesTrak's OLDEST table flag uses
# 3.5 days as a data-review aid for active satellites; that flag is also
# not a safety threshold. 72 hours is this preview's own label.
DEFAULT_EPOCH_STALE_S = 72 * 3600
# How long after a successful download the retrieval is marked older than
# the display setting. Independent of element epoch age.
DEFAULT_RETRIEVAL_STALE_S = 6 * 3600
# NOAA index cache. The files update on NOAA's schedule; this only limits
# how often this process requests them.
DEFAULT_ENVIRONMENT_CACHE_S = 30 * 60

# Geometric approach list. Approaches farther than this are omitted from the
# short list. Omission is not a probability and not a clearance.
DEFAULT_GEOMETRIC_SCREEN_KM = 25.0
MAX_SCREEN_MINUTES = 360.0
MIN_SCREEN_STEP_S = 15.0

CELESTRAK_STATIONS_URL = (
    "https://celestrak.org/NORAD/elements/gp.php?GROUP=stations&FORMAT=JSON"
)
SPACETRACK_DOC = "https://www.space-track.org/documentation"
NOAA_JSON_INDEX = "https://services.swpc.noaa.gov/json/"
NOAA_F107_URL = "https://services.swpc.noaa.gov/json/f107_cm_flux.json"
NOAA_KP_URL = "https://services.swpc.noaa.gov/json/planetary_k_index_1m.json"

# Allowed public batch in this preview. One group request, not a per-object
# catalog walk, and not the large active/Starlink groups.
ALLOWED_PUBLIC_GROUP = "stations"

USER_AGENT = "OrbitGuard/0.1 (public read-only GP and SWPC; cached)"

# One CelesTrak read. Hugging Face egress to celestrak.org has been observed
# to hang, so the client waits long enough to tell a timeout from a fast 403
# and then stops. This is not a reason to open extra connections.
CELESTRAK_TIMEOUT_S = 45.0

F107_UNIT = "sfu (10^-22 W m^-2 Hz^-1)"
KP_UNIT = "1 (dimensionless)"
TEC_UNIT = "TECU (10^16 electrons m^-2)"
DENSITY_UNIT = "kg m^-3"
