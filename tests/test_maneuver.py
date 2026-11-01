"""Unit tests for maneuver solver and propulsion budget calculations."""

import datetime
import numpy as np
import pytest

from orbitguard.physics.constants import MU_EARTH, R_EARTH
from orbitguard.physics.elements import OrbitalElements, coe_to_rv
from orbitguard.physics.maneuver_solver import ManeuverSolver
from orbitguard.physics.propagator import OrbitPropagator
from orbitguard.physics.screening import ConjunctionEvent


def test_tsiolkovsky_propellant_budget():
    """Verify rocket equation computation for cold gas CubeSat thruster."""
    prop1 = OrbitPropagator(include_j2=False, include_drag=False)
    prop2 = OrbitPropagator(include_j2=False, include_drag=False)

    solver = ManeuverSolver(
        propagator_primary=prop1,
        propagator_secondary=prop2,
        dry_mass_kg=4.0,
        fuel_mass_kg=0.5,
        thrust_n=0.050,
        isp_s=70.0,
    )

    # 0.2 m/s burn
    burn_dur, fuel_used, rem_fuel, rem_dv = solver.compute_propellant_budget(dv_mag=0.20, current_fuel_kg=0.5)

    assert fuel_used > 0.0
    assert fuel_used < 0.010  # Less than 10 grams of fuel for 0.2 m/s
    assert burn_dur > 0.0
    assert rem_fuel < 0.5
    assert rem_dv > 0.0


def test_maneuver_clearance_expansion():
    """Verify that an along-track burn executed before TCA expands miss distance."""
    coe1 = OrbitalElements(
        a=R_EARTH + 500000.0,
        e=0.001,
        i=np.radians(51.64),
        raan=np.radians(120.0),
        arg_pe=0.0,
        true_anomaly=0.0,
    )
    r1, v1 = coe_to_rv(coe1)

    # Secondary placed along encounter path
    r2 = r1 + np.array([500.0, 0.0, 0.0])  # 500 m miss distance
    v2 = v1 + np.array([0.0, -0.1, 0.0])

    prop1 = OrbitPropagator(include_j2=False, include_drag=False)
    prop2 = OrbitPropagator(include_j2=False, include_drag=False)

    solver = ManeuverSolver(
        propagator_primary=prop1,
        propagator_secondary=prop2,
        dry_mass_kg=4.0,
        fuel_mass_kg=0.5,
        thrust_n=0.050,
        isp_s=70.0,
    )

    now = datetime.datetime.now(datetime.timezone.utc)
    tca_s = 7200.0  # 2 hours away

    event = ConjunctionEvent(
        tca_seconds=tca_s,
        tca_datetime=now + datetime.timedelta(seconds=tca_s),
        miss_distance_km=0.5,
        radial_miss_km=0.5,
        in_track_miss_km=0.0,
        cross_track_miss_km=0.0,
        relative_speed_km_s=7.6,
        r1_tca=r1,
        v1_tca=v1,
        r2_tca=r2,
        v2_tca=v2,
        risk_tier="CRITICAL",
        collision_probability=0.02,
    )

    candidates = solver.solve_mitigation_options(
        event=event,
        r1_0=r1,
        v1_0=v1,
        r2_0=r2,
        v2_0=v2,
        epoch_start=now,
    )

    assert len(candidates) > 0
    best = candidates[0]
    # Post miss distance should be significantly larger than 0.5 km
    assert best.post_miss_distance_km > 0.5
    assert best.propellant_consumed_kg > 0.0
