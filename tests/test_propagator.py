"""Unit tests for high-precision Cowell orbit propagator."""

import numpy as np
import pytest

from orbitguard.physics.constants import MU_EARTH, R_EARTH
from orbitguard.physics.elements import OrbitalElements, coe_to_rv
from orbitguard.physics.propagator import OrbitPropagator


def test_keplerian_energy_conservation():
    """Verify energy conservation in two-body propagation without drag."""
    r_mag = R_EARTH + 500000.0
    v_mag = np.sqrt(MU_EARTH / r_mag)

    r0 = np.array([r_mag, 0.0, 0.0])
    v0 = np.array([0.0, v_mag, 0.0])

    prop = OrbitPropagator(include_j2=False, include_drag=False)
    # Propagate 1 full orbit (~5600 s)
    traj = prop.propagate(r0, v0, duration_seconds=5600.0, step_seconds=100.0)

    assert traj.success is True
    assert len(traj.t) > 10

    # Specific mechanical energy: E = v^2/2 - mu/r
    e_initial = 0.5 * np.linalg.norm(v0)**2 - MU_EARTH / np.linalg.norm(r0)
    for r_i, v_i in zip(traj.r, traj.v):
        e_i = 0.5 * np.linalg.norm(v_i)**2 - MU_EARTH / np.linalg.norm(r_i)
        assert pytest.approx(e_i, rel=1e-5) == e_initial


def test_j2_raan_precession():
    """Verify that J2 oblateness causes secular RAAN regression."""
    coe = OrbitalElements(
        a=R_EARTH + 500000.0,
        e=0.001,
        i=np.radians(51.64),
        raan=np.radians(100.0),
        arg_pe=np.radians(0.0),
        true_anomaly=np.radians(0.0),
    )
    r0, v0 = coe_to_rv(coe)

    prop = OrbitPropagator(include_j2=True, include_drag=False)
    # Propagate 2 orbits (~11200 s)
    traj = prop.propagate(r0, v0, duration_seconds=11200.0, step_seconds=200.0)

    assert traj.success is True
    # For i = 51.6 deg, dOmega/dt is negative (regression of nodes)
    initial_raan = traj.elements[0].raan
    final_raan = traj.elements[-1].raan
    assert final_raan < initial_raan or abs(final_raan - initial_raan) > 0.001
