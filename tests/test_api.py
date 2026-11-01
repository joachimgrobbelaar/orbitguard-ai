"""Integration tests for OrbitGuard AI FastAPI endpoints."""

import pytest
from httpx import ASGITransport, AsyncClient

from orbitguard.main import app


@pytest.mark.asyncio
async def test_health_check_endpoint():
    """Verify /api/v1/health responds with engine status."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.get("/api/v1/health")
        assert res.status_code == 200
        data = res.json()
        assert data["status"] == "healthy"
        assert "Runge-Kutta" in data["astrodynamics_engine"]["integrator"]


@pytest.mark.asyncio
async def test_satellites_list():
    """Verify seeded satellite list."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.get("/api/v1/satellites")
        assert res.status_code == 200
        data = res.json()
        assert isinstance(data, list)
        assert len(data) >= 1
        assert "AeroCube" in data[0]["name"]


@pytest.mark.asyncio
async def test_threat_simulation_endpoint():
    """Verify live threat simulation and autonomous resolution."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        payload = {
            "satellite_name": "AeroCube-3U",
            "threat_debris_name": "COSMOS-2251 FRAGMENT [CAT# 34123]",
            "miss_distance_target_km": 0.85,
            "tca_lead_hours": 12.0,
        }
        res = await client.post("/api/v1/demo/simulate-threat", json=payload)
        assert res.status_code == 200
        data = res.json()
        assert data["success"] is True
        report = data["simulation_report"]
        assert report["nominal_encounter"]["risk_tier"] in ("CRITICAL", "HIGH")
        assert report["autonomous_resolution"]["post_maneuver_miss_distance_km"] > 5.0
        assert report["autonomous_resolution"]["propellant_consumed_grams"] > 0.0


@pytest.mark.asyncio
async def test_screening_cycle_sweep():
    """Verify screening sweep across catalog."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Run 2h screening horizon
        payload = {
            "horizon_hours": 2.0,
        }
        res = await client.post("/api/v1/conjunctions/screen", json=payload)
        assert res.status_code == 200
        data = res.json()
        assert isinstance(data, list)
