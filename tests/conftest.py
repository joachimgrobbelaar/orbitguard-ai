"""Pytest configuration for OrbitGuard tests.

Disables the background tracking loop thread and live network fetches by
default so tests execute deterministically and offline against committed
snapshots or explicit test transports.
"""

from __future__ import annotations

import os
import pytest

os.environ.setdefault("ORBITGUARD_LOOP_ENABLED", "0")
os.environ.setdefault("ORBITGUARD_LOOP_LIVE_FETCH", "0")


@pytest.fixture(autouse=True)
def _disable_background_loop_and_network(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ORBITGUARD_LOOP_ENABLED", "0")
    monkeypatch.setenv("ORBITGUARD_LOOP_LIVE_FETCH", "0")
