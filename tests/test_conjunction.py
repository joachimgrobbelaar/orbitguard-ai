"""Unit tests for conjunction detection and collision probability."""

import datetime
import numpy as np
import pytest

from orbitguard.physics.collision_probability import calculate_collision_probability, evaluate_encounter_risk
from orbitguard.physics.constants import MU_EARTH, R_EARTH
from orbitguard.physics.elements import OrbitalElements, coe_to_rv
from orbitguard.physics.propagator import OrbitPropagator
from orbitguard.physics.screening import ConjunctionScreener


def test_collision_probability_calculation():
    """Verify Akella-Alfriend Pc model behavior."""
    # When miss distance is 0, Pc should be at local maximum
    pc_zero = calculate_collision_probability(miss_distance_m=0.0, combined_radius_m=2.0)
    assert pc_zero > 0.0

    # Miss distance of 200m
    pc_200m = calculate_collision_probability(miss_distance_m=200.0, combined_radius_m=2.0)
    assert 0.0 < pc_200m < pc_zero

    # Miss distance of 50 km should be virtually 0
    pc_50km = calculate_collision_probability(miss_distance_m=50000.0, combined_radius_m=2.0)
    assert pc_50km == 0.0


def test_conjunction_screening_detection():
    """Verify screener identifies synthetic close approach encounter."""
    coe1 = OrbitalElements(
        a=R_EARTH + 500000.0,
        e=0.001,
        i=np.radians(51.64),
        raan=np.radians(120.0),
        arg_pe=0.0,
        true_anomaly=0.0,
    )
    r1, v1 = coe_to_rv(coe1)

    # Place secondary near primary with slight velocity divergence
    r2 = r1 + np.array([2000.0, 500.0, 0.0])  # ~2.06 km away
    v2 = v1 + np.array([0.0, -0.5, 0.2])

    prop1 = OrbitPropagator(include_j2=False, include_drag=False)
    prop2 = OrbitPropagator(include_j2=False, include_drag=False)

    screener = ConjunctionScreener(prop1, prop2, screening_threshold_km=10.0)
    now = datetime.datetime.now(datetime.timezone.utc)

    # Screen over 2000 seconds
    events = screener.screen_encounter(
        r1_0=r1,
        v1_0=v1,
        r2_0=r2,
        v2_0=v2,
        epoch_start=now,
        duration_hours=0.5,
        coarse_step_s=30.0,
    )

    assert len(events) >= 1
    ev = events[0]
    assert ev.miss_distance_km <= 10.0
    assert ev.risk_tier in ("CRITICAL", "HIGH", "MODERATE", "LOW")
