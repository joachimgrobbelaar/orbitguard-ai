"""OrbitGuard FastAPI entrypoint.

Serves the monitoring API and the mission-control page. It does not compute
a collision probability, recommend a maneuver, or command a spacecraft.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
import logging
import os
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse

from orbitguard import __version__
from orbitguard.config import settings
from orbitguard.monitor.api import get_loop, router as monitor_router
from orbitguard.monitor.loop import is_loop_enabled
from orbitguard.nd import api_status

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("orbitguard")

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
INDEX_PATH = STATIC_DIR / "index.html"
MANIFEST_PATH = STATIC_DIR / "manifest.webmanifest"
SW_PATH = STATIC_DIR / "sw.js"
ICONS_DIR = STATIC_DIR / "icons"
APPLE_TOUCH_ICON_PATH = ICONS_DIR / "apple-touch-icon.png"

_ICON_MEDIA_TYPES = {
    ".png": "image/png",
    ".svg": "image/svg+xml",
}


@asynccontextmanager
async def lifespan(_app: FastAPI):
    loop = get_loop()
    if is_loop_enabled():
        loop.start()
    try:
        yield
    finally:
        loop.stop()


app = FastAPI(
    title=settings.PROJECT_NAME,
    version=settings.VERSION,
    description=(
        "Human-reviewed LEO monitoring. Public GP states, NOAA indices, and "
        "geometric miss distance. Collision probability is not computed."
    ),
    lifespan=lifespan,
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


@app.get("/manifest.webmanifest")
def manifest():
    if not MANIFEST_PATH.is_file():
        raise HTTPException(status_code=404, detail="Manifest not found")
    return FileResponse(MANIFEST_PATH, media_type="application/manifest+json")


@app.get("/sw.js")
def service_worker():
    if not SW_PATH.is_file():
        raise HTTPException(status_code=404, detail="Service worker not found")
    return FileResponse(
        SW_PATH,
        media_type="application/javascript",
        headers={
            "Service-Worker-Allowed": "/",
            "Cache-Control": "no-cache",
        },
    )


@app.get("/apple-touch-icon.png")
def apple_touch_icon():
    if not APPLE_TOUCH_ICON_PATH.is_file():
        raise HTTPException(status_code=404, detail="apple-touch-icon.png not found")
    return FileResponse(APPLE_TOUCH_ICON_PATH, media_type="image/png")


@app.get("/icons/{name}")
@app.get("/icons/{name:path}", include_in_schema=False)
def icon_file(name: str):
    if (
        not name
        or ".." in name
        or "/" in name
        or "\\" in name
        or "\x00" in name
        or name.startswith(".")
        or Path(name).name != name
    ):
        raise HTTPException(status_code=400, detail="Invalid icon path")
    icons_root = ICONS_DIR.resolve()
    candidate = (icons_root / name).resolve()
    if candidate.parent != icons_root or not candidate.is_relative_to(icons_root):
        raise HTTPException(status_code=400, detail="Invalid icon path")
    media_type = _ICON_MEDIA_TYPES.get(candidate.suffix.lower())
    if media_type is None or not candidate.is_file():
        raise HTTPException(status_code=404, detail="Icon not found")
    return FileResponse(candidate, media_type=media_type)


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", settings.PORT))
    uvicorn.run("orbitguard.main:app", host=settings.HOST, port=port, reload=settings.DEBUG)
