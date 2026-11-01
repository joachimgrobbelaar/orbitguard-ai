"""Pytest test configuration and database fixtures for OrbitGuard AI."""

import pytest
import pytest_asyncio

from orbitguard.db.session import init_db


@pytest_asyncio.fixture(autouse=True)
async def setup_test_db():
    """Ensure database schema is initialized and seeded before running tests."""
    await init_db()
    yield
