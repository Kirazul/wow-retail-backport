"""Regression tests for midnight_physbake.py (retail .phys settled offline).

    python -m unittest tools/tests/midnight_physbake_test.py      (from the repo root)

Every angular constraint must move a joint TOWARDS its target: a correction of
the wrong sign is an anti-spring, and a chain held by one explodes (the weld
and the shoulder motor both did, 2026-10-09).
"""

from __future__ import annotations
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
import _toolpath  # noqa: E402,F401

import math
import sys
import unittest

import numpy as np


import physbake as PB  # noqa: E402

H2 = (1.0 / (PB.FPS * PB.SUBSTEPS)) ** 2


def q_axis(ax, deg):
    ax = np.asarray(ax, float)
    ax = ax / np.linalg.norm(ax)
    a = math.radians(deg)
    return np.array([math.cos(a / 2), *(math.sin(a / 2) * ax)])


def joint(t, **rec):
    """A kinematic body A and a free body B joined at their origins, frames aligned."""
    r = {"type": t, "a": 0, "b": 1, "frameA": np.eye(4), "frameB": np.eye(4)}
    r.update(rec)
    b = [PB.Body({"bone": i, "pos": np.zeros(3)}, [], kinematic=(i == 0)) for i in (0, 1)]
    return PB.Joint(r, b), b[1]


def misalign(j):
    A, B = j.axes()
    dq = PB.qmul(PB.mat_q(A), PB.qconj(PB.mat_q(B)))
    return 2 * math.degrees(math.acos(min(1.0, abs(dq[0]))))


def z_angle(j):
    A, B = j.axes()
    return math.degrees(math.acos(max(-1.0, min(1.0, float(np.dot(A[:, 2], B[:, 2]))))))


class ConstraintDirection(unittest.TestCase):
    def test_rigid_weld_closes(self):
        j, b = joint(2, freq=0.0, damp=0.0)
        b.q = q_axis([1, 0.3, 0], 20)
        j.solve(H2, True)
        self.assertLess(misalign(j), 2.0)

    def test_shoulder_motor_closes(self):
        j, b = joint(1, twist=(-180.0, 180.0), cone=math.pi,
                     motor={"max": 1e9, "mode": 1, "freq": 0.0, "damp": 0.0})
        b.q = q_axis([1, 0.3, 0], 20)
        j.motor(H2)
        self.assertLess(misalign(j), 2.0)

    def test_revolute_motor_closes(self):
        j, b = joint(3, limits=(-180.0, 180.0), motor={"max": 1e9, "mode": 1, "freq": 0.0, "damp": 0.0})
        b.q = q_axis([0, 0, 1], 40)
        j.motor(H2)
        self.assertLess(misalign(j), 10.0)

    def test_cone_limit_closes(self):
        j, b = joint(1, twist=(-180.0, 180.0), cone=math.radians(10))
        b.q = q_axis([1, 0, 0], 40)
        j.solve(H2, True)
        self.assertLess(z_angle(j), 12.0)

    def test_twist_limit_closes(self):
        j, b = joint(1, twist=(-5.0, 5.0), cone=math.pi)
        b.q = q_axis([0, 0, 1], 40)
        j.solve(H2, True)
        self.assertLess(misalign(j), 10.0)

    def test_hinge_axis_closes(self):
        j, b = joint(3, limits=(-180.0, 180.0))
        b.q = q_axis([1, 0, 0], 30)
        j.solve(H2, True)
        self.assertLess(z_angle(j), 3.0)

    def test_motor_capped_by_torque(self):
        j, b = joint(1, twist=(-180.0, 180.0), cone=math.pi,
                     motor={"max": 1e-6, "mode": 1, "freq": 0.0, "damp": 0.0})
        b.q = q_axis([1, 0, 0], 20)
        j.motor(H2)
        self.assertGreater(misalign(j), 19.9)


class RealModelsSettle(unittest.TestCase):
    """Retail models whose chains once flailed (integration: read from the
    local retail cache, skipped where it does not hold them)."""

    def settle(self, fdid):
        import physread as PH
        try:
            blob = PH.physics_blob(fdid)
        except Exception:
            blob = None
        if not blob:
            self.skipTest("retail model %d is not in the local cache" % fdid)
        return PB.settle(PH.parse(blob))

    def test_staff_ornament_comes_to_rest(self):
        # staff_2h_raidmidnight_d_01: two polytope links, centres off their
        # origins - turning about the origin instead of the centre pumped
        # energy in until they flailed (chatter 0.04 yd/frame)
        _pose, err, _rest, chatter, drift = self.settle(7133429)
        self.assertLess(drift, 2e-3)
        self.assertLess(chatter, 2e-3)
        self.assertLess(err, 0.02)

    def test_plume_with_floating_tip_rests(self):
        # helm_mail_outdoorvoidelf_c_01_hu_f: diagonal capsules (full inertia
        # tensor), tip gravity -0.5 - it exploded before both fixes
        _pose, err, _rest, _chatter, drift = self.settle(6758489)
        self.assertLess(drift, 2e-3)
        self.assertLess(err, 0.02)


class InertiaFollowsTheShape(unittest.TestCase):
    def test_diagonal_capsule_is_hard_to_spin_across_its_axis(self):
        a, b = np.zeros(3), np.array([-0.3, 0.0, 0.3])
        body = PB.Body({"bone": 1, "pos": np.zeros(3)},
                       [{"density": 1000.0, "capsule": (a, b, 0.03)}], kinematic=False)
        u = (b - a) / np.linalg.norm(b - a)
        across = np.cross(u, [0.0, 1.0, 0.0])
        self.assertGreater(1.0 / body.world_invI(across), 10.0 / body.world_invI(u))


if __name__ == "__main__":
    unittest.main()
