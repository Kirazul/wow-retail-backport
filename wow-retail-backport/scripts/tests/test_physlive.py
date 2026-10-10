"""Runtime retail physics, the reference (physlive.py): what a client mod's
solver must do, frame by frame, from the wearer's real motion.

    python bp.py selftest
"""
from __future__ import annotations
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
import _toolpath  # noqa: E402,F401
import math
import unittest

import numpy as np

import physlive as R  # noqa: E402

DT = 1.0 / 60


def frame(R3=None, t=(0.0, 0.0, 0.0), s=1.0):
    M = np.eye(4)
    M[:3, :3] = (np.eye(3) if R3 is None else R3) * s
    M[:3, 3] = t
    return M


def rot_y(deg):
    a = math.radians(deg)
    return np.array([[math.cos(a), 0, math.sin(a)], [0, 1, 0], [-math.sin(a), 0, math.cos(a)]])


def chain(cone_deg=60.0, motor=None, links=2, horizontal=False, root_capsule=None):
    """A kinematic root (bone 0) and `links` capsule links hanging from it
    (bones 1..), each 0.1 yd long, joined by shoulder joints whose frame z
    runs along the link (down, or +x when `horizontal`)."""
    d = np.array([0.1, 0, 0]) if horizontal else np.array([0, 0, -0.1])
    z = d / 0.1
    x = np.array([0, 0, 1.0]) if horizontal else np.array([1.0, 0, 0])
    Rf = np.column_stack([x, np.cross(z, x), z])
    bodies = [{"type": 0, "bone": 0, "pos": np.zeros(3), "shape0": 0, "nshape": 1 if root_capsule else 0,
               "gravity": 1.0, "drag": 0.0, "angdrag": 0.0}]
    shapes = [root_capsule] if root_capsule else []
    joints = []
    for i in range(links):
        bodies.append({"type": 1, "bone": i + 1, "pos": d * i, "shape0": len(shapes), "nshape": 1,
                       "gravity": 1.0, "drag": 0.0, "angdrag": 0.0})
        shapes.append({"type": 1, "index": 0, "friction": 0.5, "restitution": 0.0, "density": 1100.0,
                       "capsule": (np.zeros(3), d.copy(), 0.01)})
        fa, fb = np.eye(4), np.eye(4)
        fa[:3, :3] = Rf
        fb[:3, :3] = Rf
        fa[:3, 3] = d if i else np.zeros(3)
        j = {"a": i, "b": i + 1, "type": 1, "id": i, "frameA": fa, "frameB": fb,
             "twist": (-10.0, 10.0), "cone": math.radians(cone_deg)}
        if motor:
            j["motor"] = dict(motor)
        joints.append(j)
    return {"version": 6, "bodies": bodies, "shapes": shapes, "joints": joints}


def run(model, root_at, seconds, start=0.0):
    """Step `model` for `seconds` with bone 0 (the kinematic root) at
    root_at(t); every body bone starts at its bind pose on that root."""
    out = None
    n = int(round(seconds / DT))
    for k in range(n):
        t = start + k * DT
        M = root_at(t)
        out = model.step({b: M for b in model.bones()}, DT)
    return out


def tip_world(model):
    b = model.bodies[-1]
    return b.x + R.PB.qrot(b.q, np.array([0, 0, -0.1])) if True else None


class Hanging(unittest.TestCase):
    def test_horizontal_chain_falls_and_rests_inside_its_cone(self):
        m = R.Model(chain(cone_deg=60.0, horizontal=True))
        run(m, lambda t: frame(), 6.0)
        for j in m.joints:
            A, B = j.axes()
            ang = math.degrees(math.acos(max(-1.0, min(1.0, float(np.dot(A[:, 2], B[:, 2]))))))
            self.assertLess(ang, 60.0 + 1.5)
        # it fell: the first link points down at its cone's edge
        b1 = m.bodies[1]
        down = R.PB.qrot(b1.q, np.array([1.0, 0, 0]))
        self.assertLess(down[2], -0.8)
        self.assertLess(max(np.linalg.norm(b.v) for b in m.bodies[1:]), 1e-2)

    def test_position_motor_holds_the_authored_angle(self):
        m = R.Model(chain(cone_deg=30.0, horizontal=True, links=1,
                          motor={"max": 1000.0, "mode": 1, "freq": 8.0, "damp": 1.0}))
        run(m, lambda t: frame(), 3.0)
        d = R.PB.qrot(m.bodies[1].q, np.array([1.0, 0, 0]))
        sag = math.degrees(math.asin(-d[2]))
        # the spring is on the link's own (centre of mass) inertia, Box2D /
        # Domino soft constraints: k = w^2 m L^2 / 12 against m g L / 2 ->
        # sag = 6 g / (w^2 L) = 13.7 deg here; the point joint stiffens it a little
        w = 2 * math.pi * 8.0
        self.assertLess(sag, math.degrees(6 * 10.0 / (w * w * 0.1)))
        self.assertGreater(sag, 5.0)       # and it IS a spring, not a weld

    def test_bone_out_is_the_body_pose_over_its_bind(self):
        m = R.Model(chain(horizontal=True))
        out = run(m, lambda t: frame(t=(5.0, 1.0, 2.0)), 0.5)
        b = m.bodies[2]
        M = out[2]
        self.assertTrue(np.allclose(M[:3, :3], R.PB.qmat(b.q), atol=1e-9))
        bind_origin = np.array([0.1, 0, 0])
        np.testing.assert_allclose(M[:3, :3] @ bind_origin + M[:3, 3], b.x, atol=1e-9)
        self.assertNotIn(0, out)            # kinematic bones are the client's


class Contacts(unittest.TestCase):
    def test_link_rests_on_the_kinematic_shell_not_inside_it(self):
        # a 4 cm capsule across the horizontal chain, under its SECOND link
        # (joined to the first, not to the root that carries the shell)
        a2, b2 = np.array([0.15, -0.1, -0.06]), np.array([0.15, 0.1, -0.06])
        shell = {"type": 1, "index": 0, "friction": 0.5, "restitution": 0.0, "density": 1000.0,
                 "capsule": (a2, b2, 0.04)}
        m = R.Model(chain(cone_deg=90.0, horizontal=True, links=2, root_capsule=shell))
        run(m, lambda t: frame(), 6.0)
        b = m.bodies[2]
        e = b.x + R.PB.qrot(b.q, np.array([0.1, 0, 0]))
        self.assertGreater(R.seg_seg_distance(b.x, e, a2, b2), 0.01 + 0.04 - 1e-3)
        free = R.Model(chain(cone_deg=90.0, horizontal=True, links=2))
        run(free, lambda t: frame(), 6.0)
        fb = free.bodies[2]
        fe = fb.x + R.PB.qrot(fb.q, np.array([0.1, 0, 0]))
        self.assertLess(fe[2], e[2] - 0.02)  # without the shell it hangs lower

    def test_joined_bodies_do_not_collide(self):
        # retail authors a plume's first link INSIDE its helm's shell capsule
        # (a raid helm measured: 13 cm into an 18 cm shell): bodies a joint connects
        # never collide (Box2D collideConnected = false, PhysX / Havok alike)
        shell = {"type": 1, "index": 0, "friction": 0.5, "restitution": 0.0, "density": 1000.0,
                 "capsule": (np.array([0.0, 0.0, 0.0]), np.array([0.0, 0.0, 0.0]), 0.2)}
        m = R.Model(chain(cone_deg=30.0, horizontal=True, links=1, root_capsule=shell,
                          motor={"max": 1000.0, "mode": 1, "freq": 8.0, "damp": 1.0}))
        run(m, lambda t: frame(), 2.0)
        np.testing.assert_allclose(m.bodies[1].x, [0.0, 0.0, 0.0], atol=1e-3)   # still on its joint


class Motion(unittest.TestCase):
    def test_running_root_swings_the_chain_back_then_it_recovers(self):
        m = R.Model(chain(cone_deg=80.0, links=2))
        run(m, lambda t: frame(), 10.0)
        rest = m.bodies[-1].x.copy()
        lag = []

        def moving(t):
            return frame(t=(min(t - 10.0, 1.0) * 7.0, 0, 0))     # 7 yd/s for one second
        for k in range(60):
            t = 10.0 + k * DT
            M = moving(t)
            m.step({b: M for b in m.bones()}, DT)
            lag.append(M[0, 3] - m.bodies[-1].x[0])
        self.assertGreater(max(lag), 0.02)                      # the tip trails the run
        run(m, lambda t: frame(t=(7.0, 0, 0)), 10.0, start=11.0)
        np.testing.assert_allclose(m.bodies[-1].x, rest + [7.0, 0, 0], atol=5e-3)

    def test_teleport_resets_to_bind_on_the_new_root(self):
        m = R.Model(chain(horizontal=True))
        run(m, lambda t: frame(), 1.0)
        out = m.step({b: frame(t=(100.0, 0, 0)) for b in m.bones()}, DT)
        np.testing.assert_allclose(out[2][:3, 3], [100.0, 0, 0], atol=1e-9)   # bind: identity on the root
        np.testing.assert_allclose(out[2][:3, :3], np.eye(3), atol=1e-9)

    def test_long_gap_resets(self):
        m = R.Model(chain(horizontal=True))
        run(m, lambda t: frame(), 1.0)
        out = m.step({b: frame() for b in m.bones()}, 1.0)
        np.testing.assert_allclose(out[1][:3, :3], np.eye(3), atol=1e-9)

    def test_scaled_model_hangs_at_the_same_angles(self):
        a = R.Model(chain(horizontal=True))
        b = R.Model(chain(horizontal=True))
        run(a, lambda t: frame(), 12.0)       # both at rest (gravity is 10 / s in model units)
        run(b, lambda t: frame(s=2.0), 12.0)
        for x, y in zip(a.bodies[1:], b.bodies[1:]):
            self.assertGreater(abs(float(np.dot(x.q, y.q))), 0.9999)

    def test_turned_root_hangs_by_world_gravity(self):
        m = R.Model(chain(cone_deg=170.0, links=1))
        run(m, lambda t: frame(R3=rot_y(90.0)), 5.0)       # the model lies on its side
        d = R.PB.qrot(m.bodies[1].q, np.array([0, 0, -1.0]))  # the link's bind "down", now in world
        self.assertLess(d[2], -0.95)                         # still hangs down in the world

    def test_jitter_stays_finite(self):
        m = R.Model(chain(cone_deg=60.0, links=3, motor={"max": 50.0, "mode": 1, "freq": 4.0, "damp": 0.5}))
        rng = np.random.default_rng(3)
        for k in range(3000):
            M = frame(R3=rot_y(float(rng.normal(0, 15))), t=rng.normal(0, 0.3, 3))
            out = m.step({b: M for b in m.bones()}, DT)
            self.assertTrue(all(np.isfinite(v).all() for v in out.values()))


if __name__ == "__main__":
    unittest.main()
