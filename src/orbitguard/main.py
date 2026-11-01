"""OrbitGuard AI — Main FastAPI Application Entrypoint."""

from contextlib import asynccontextmanager
import logging
import os
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from orbitguard.api.routers import alerts, conjunctions, maneuvers, satellites, simulation
from orbitguard.config import settings
from orbitguard.db.session import init_db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("orbitguard")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application startup and shutdown lifecycle management."""
    logger.info("Initializing OrbitGuard AI Database and Ephemeris Engine...")
    await init_db()
    logger.info("OrbitGuard AI Core Engine online on port %d", settings.PORT)
    yield
    logger.info("OrbitGuard AI shutting down.")


app = FastAPI(
    title=settings.PROJECT_NAME,
    version=settings.VERSION,
    description="Autonomous Orbital Conjunction Screening & Maneuver Recommendation Copilot for CubeSats & Small Satellites.",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Register routers under /api/v1 and /api/v1/orbitguard alias
for prefix in ("/api/v1", "/api/v1/orbitguard"):
    app.include_router(satellites.router, prefix=prefix)
    app.include_router(conjunctions.router, prefix=prefix)
    app.include_router(maneuvers.router, prefix=prefix)
    app.include_router(alerts.router, prefix=prefix)
    app.include_router(simulation.router, prefix=prefix)

if os.path.exists(STATIC_DIR):
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/api/v1/health")
@app.get("/api/v1/orbitguard/health")
async def health_check():
    """System health and astrodynamics engine telemetry."""
    return {
        "status": "healthy",
        "service": "OrbitGuard AI",
        "version": settings.VERSION,
        "astrodynamics_engine": {
            "integrator": "Runge-Kutta 4(5) Cowell",
            "perturbations": ["J2 Earth Oblateness", "Exponential Atmospheric Drag"],
            "screening_horizon_hours": settings.SCREENING_WINDOW_HOURS,
            "conjunction_trigger_km": settings.CONJUNCTION_THRESHOLD_KM,
            "maneuver_solver": "VNC Phasing & Out-of-Plane Tsiolkovsky Engine",
        },
    }


@app.get("/")
async def root():
    """Serve 3D Mission Control Copilot dashboard or API overview."""
    index_path = os.path.join(STATIC_DIR, "index.html")
    if os.path.exists(index_path):
        return FileResponse(index_path)
    return {
        "message": "OrbitGuard AI Mission Control Copilot is active.",
        "docs_url": "/docs",
    }


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", settings.PORT))
    uvicorn.run("orbitguard.main:app", host=settings.HOST, port=port, reload=settings.DEBUG)
