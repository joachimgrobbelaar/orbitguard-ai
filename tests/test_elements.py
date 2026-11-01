"""Unit tests for orbital elements transformations and VNC frame basis."""

import math
import numpy as np
import pytest

from orbitguard.physics.constants import MU_EARTH, R_EARTH
from orbitguard.physics.elements import OrbitalElements, coe_to_rv, compute_vnc_basis, rv_to_coe


def test_coe_to_rv_and_back():
    """Verify round-trip consistency of Keplerian elements to Cartesian and back."""
    # 500 km altitude circular orbit at 51.6 deg inclination
    a = R_EARTH + 500000.0
    e = 0.005
    i = math.radians(51.6)
    raan = math.radians(120.0)
    arg_pe = math.radians(45.0)
    nu = math.radians(30.0)

    orig_coe = OrbitalElements(a=a, e=e, i=i, raan=raan, arg_pe=arg_pe, true_anomaly=nu)
    r, v = coe_to_rv(orig_coe)

    # Reconstruct COE
    rec_coe = rv_to_coe(r, v)

    assert pytest.approx(rec_coe.a, rel=1e-5) == a
    assert pytest.approx(rec_coe.e, abs=1e-5) == e
    assert pytest.approx(rec_coe.i, abs=1e-5) == i
    assert pytest.approx(rec_coe.raan, abs=1e-5) == raan
    assert pytest.approx(rec_coe.arg_pe, abs=1e-5) == arg_pe
    assert pytest.approx(rec_coe.true_anomaly, abs=1e-4) == nu


def test_vnc_basis_orthonormality():
    """Verify that VNC triad is mutually perpendicular and unit length."""
    r = np.array([7000000.0, 0.0, 0.0])
    v = np.array([0.0, 7500.0, 1000.0])

    u_v, u_n, u_c = compute_vnc_basis(r, v)

    assert pytest.approx(np.linalg.norm(u_v), abs=1e-7) == 1.0
    assert pytest.approx(np.linalg.norm(u_n), abs=1e-7) == 1.0
    assert pytest.approx(np.linalg.norm(u_c), abs=1e-7) == 1.0

    assert pytest.approx(np.dot(u_v, u_n), abs=1e-7) == 0.0
    assert pytest.approx(np.dot(u_v, u_c), abs=1e-7) == 0.0
    assert pytest.approx(np.dot(u_n, u_c), abs=1e-7) == 0.0
