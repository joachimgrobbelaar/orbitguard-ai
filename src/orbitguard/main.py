"""OrbitGuard FastAPI entrypoint.

Serves the monitoring API and the mission-control page. It does not compute
a collision probability, recommend a maneuver, or command a spacecraft.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse

from orbitguard import __version__
from orbitguard.config import settings
from orbitguard.monitor.api import router as monitor_router
from orbitguard.nd import api_status

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("orbitguard")

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
INDEX_PATH = STATIC_DIR / "index.html"

app = FastAPI(
    title=settings.PROJECT_NAME,
    version=settings.VERSION,
    description=(
        "Human-reviewed LEO monitoring. Public GP states, NOAA indices, and "
        "geometric miss distance. Collision probability is not computed."
    ),
)
app.include_router(monitor_router)


@app.get("/health")
@app.get("/api/v1/health")
@app.get("/api/orbitguard/health")
def health() -> dict:
    """Process health. Propagation availability is reported, not invented."""
    nano = api_status()
    return {
        "status": "healthy",
        "service": "OrbitGuard",
        "version": __version__,
        "role": "Human-reviewed LEO monitoring. This service does not command a spacecraft.",
        "collision_probability": "not_computable",
        "expected_collision_count": "not_implemented",
        "nanodynamics": nano,
    }


@app.get("/")
def root():
    if INDEX_PATH.is_file():
        return FileResponse(INDEX_PATH)
    return {"message": "OrbitGuard monitoring API is active.", "docs_url": "/docs"}


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", settings.PORT))
    uvicorn.run("orbitguard.main:app", host=settings.HOST, port=port, reload=settings.DEBUG)
