"""Physics contacts: a moving part rests ON the wearer's stand-in shape.

    python bp.py selftest

In game (2026-10-09) offline-settled helms hung beside the head: the solver
had no contacts, so every part fell through the kinematic proxy shapes the
.phys file carries for the wearer (255 capsules measured on 304 retail models).
"""
from __future__ import annotations

import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import _toolpath  # noqa: E402,F401

import physbake as PB  # noqa: E402


def scene(gap=0.4):
    """A horizontal kinematic capsule (the 'head', r 0.10) at the origin and a
    free dynamic capsule (r 0.05) `gap` above it."""
    return {
        "version": 6,
        "bodies": [
            {"type": 0, "bone": 0, "pos": np.zeros(3), "shape0": 0, "nshape": 1, "gravity": 1.0,
             "drag": 0.0, "angdrag": 0.0, "weight": 0.0, "tag": "BDY4"},
            {"type": 1, "bone": 1, "pos": np.array([0.0, 0.0, gap]), "shape0": 1, "nshape": 1, "gravity": 1.0,
             "drag": 2.0, "angdrag": 2.0, "weight": 0.0, "tag": "BDY4"},
        ],
        "shapes": [
            {"type": 1, "index": 0, "friction": 0.5, "restitution": 0.0, "density": 1000.0,
             "capsule": (np.array([0.0, -0.3, 0.0]), np.array([0.0, 0.3, 0.0]), 0.10)},
            {"type": 1, "index": 1, "friction": 0.5, "restitution": 0.0, "density": 500.0,
             "capsule": (np.array([0.0, -0.1, 0.0]), np.array([0.0, 0.1, 0.0]), 0.05)},
        ],
        "joints": [],
    }


class Contacts(unittest.TestCase):
    def test_rests_on_the_proxy(self):
        PB.CONTACTS = True
        pose, _err, _rest, _worst, _drift = PB.settle(scene(), max_seconds=6.0)
        z = pose[1][1][2]
        self.assertAlmostEqual(z, 0.15, delta=0.01)       # 0.10 + 0.05: on top, not through

    def test_control_falls_through(self):
        PB.CONTACTS = False
        try:
            frames, _ = PB.simulate(scene(), PB.static_root, 2.0, warmup=0)
            z = frames[1][-1][1][2]
            self.assertLess(z, -1.0)                          # what the in-game bug was
        finally:
            PB.CONTACTS = True

    def test_bind_overlap_left_alone(self):
        """A part authored inside the proxy (its own anchor) is not shot out."""
        PB.CONTACTS = True
        sc = scene(gap=0.05)                                  # overlapping at bind
        frames, _ = PB.simulate(sc, PB.static_root, 0.5, warmup=0)
        z = frames[1][-1][1][2]
        self.assertLess(z, 0.05)                              # it falls (no explosive push-out)


if __name__ == "__main__":
    unittest.main()
