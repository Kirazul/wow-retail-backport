r"""
physbake.py - retail runtime physics, simulated offline from the
model's own .phys data and baked into looping bone keys on the model's
resident Stand sequence (3.3.5a has no physics; no runtime hook of ours adds
any).

    python bp.py phys --work W <model fdid> --settle    (study only; shipping a bake is OFF)

WHAT IS SIMULATED (retail's constants, the universal-modder notes + wowdev PHYS)
  gravity (0, 0, -10) yd/s^2; step 1/30 s; bodies from BDY4 (type 0 = the
  kinematic root on its bone, type 1 = dynamic and REPLACES its bone's
  transform); mass and inertia from the shapes (capsule / sphere / box /
  polytope hull box) x density; joints from JOIN: spherical (anchors meet),
  shoulder (anchors meet + cone + twist limits about the frame z; twist in
  degrees, cone in radians clamped 10..170 deg), revolute (frame z axes held
  together + hinge limits, degrees), weld (soft: frequency Hz + damping ratio),
  distance; per-body linear/angular damping v *= max(0, 1 - h*d) with the
  body's own drag.
  Solver: XPBD rigid bodies (Mueller et al. 2020), 10 substeps x 4 iterations per 1/30 s -
  stiff joints hold exactly, no explosion; every joint's error is measured
  and reported.

WHAT DRIVES IT
  The kinematic root follows the model's own animation of its bone, carried
  by the motion of the attachment point it hangs on during a real retail
  character's Stand animation, time-scaled onto the model's own Stand loop so the bake loops. The
  bake records the last of several warm-up loops (settled, periodic), and the
  first and last keys meet.

WHAT 3.3.5 CANNOT DO, SAID PLAINLY
  An attached model plays its OWN sequence, not the character's: the baked
  motion is retail's idle (settled hang + idle sway), the same in every
  stance. Retail's extra swing while running is not reproducible as data.
"""
from __future__ import annotations
import os as _os, sys as _sys

import argparse
import math
import os
import struct

import numpy as np

import physread as PH

G = np.array([0.0, 0.0, -10.0])
FPS = 30
SUBSTEPS = 10
ITERATIONS = 4             # hard-constraint passes per substep (soft ones run in the first)
WARMUP_LOOPS = 4
DEFAULT_DAMP = 0.5          # Domino's default linear/angular damping when a body sets none


# ---------------------------------------------------------------------------
# quaternions (w, x, y, z)
# ---------------------------------------------------------------------------
def qmul(a, b):
    w1, x1, y1, z1 = a
    w2, x2, y2, z2 = b
    return np.array([w1 * w2 - x1 * x2 - y1 * y2 - z1 * z2,
                     w1 * x2 + x1 * w2 + y1 * z2 - z1 * y2,
                     w1 * y2 - x1 * z2 + y1 * w2 + z1 * x2,
                     w1 * z2 + x1 * y2 - y1 * x2 + z1 * w2])


def qconj(q):
    return np.array([q[0], -q[1], -q[2], -q[3]])


def qnorm(q):
    return q / np.linalg.norm(q)


def qrot(q, v):
    return qmul(qmul(q, np.array([0.0, *v])), qconj(q))[1:]


def qmat(q):
    w, x, y, z = q
    return np.array([[1 - 2 * (y * y + z * z), 2 * (x * y - w * z), 2 * (x * z + w * y)],
                     [2 * (x * y + w * z), 1 - 2 * (x * x + z * z), 2 * (y * z - w * x)],
                     [2 * (x * z - w * y), 2 * (y * z + w * x), 1 - 2 * (x * x + y * y)]])


def mat_q(m):
    t = np.trace(m)
    if t > 0:
        s = math.sqrt(t + 1.0) * 2
        return qnorm(np.array([0.25 * s, (m[2, 1] - m[1, 2]) / s, (m[0, 2] - m[2, 0]) / s, (m[1, 0] - m[0, 1]) / s]))
    i = int(np.argmax(np.diag(m)))
    j, k = (i + 1) % 3, (i + 2) % 3
    s = math.sqrt(1.0 + m[i, i] - m[j, j] - m[k, k]) * 2
    q = np.zeros(4)
    q[0] = (m[k, j] - m[j, k]) / s
    q[1 + i] = 0.25 * s
    q[1 + j] = (m[j, i] + m[i, j]) / s
    q[1 + k] = (m[k, i] + m[i, k]) / s
    return qnorm(q)


def orthonormal(m3):
    """A frame's 3x3 with the stored per-race scale taken out (columns unit,
    right-handed: z = x cross y, as the exporter notes require)."""
    x = m3[:, 0] / np.linalg.norm(m3[:, 0])
    y = m3[:, 1] - x * np.dot(x, m3[:, 1])
    y /= np.linalg.norm(y)
    z = np.cross(x, y)
    return np.stack([x, y, z], 1)


# ---------------------------------------------------------------------------
# bodies
# ---------------------------------------------------------------------------
class Body:
    def __init__(self, rec, shapes, kinematic):
        self.rec = rec
        self.kin = kinematic
        self.bone = rec["bone"]
        # mass, centre and inertia (body local) from its shapes
        # Each part: (mass, centre, half extents along its own principal axes,
        # those axes as matrix columns). A capsule lies along a -> b, which is
        # rarely a body axis (a plume's links run diagonally), so the tensor is
        # built in the part's frame and rotated into the body's: a diagonal
        # tensor in body axes made the solver spin links about the wrong axis
        # and hard limits then pumped energy in (helm_mail_outdoorvoidelf).
        m_tot, com, parts = 0.0, np.zeros(3), []
        for s in shapes:
            den = max(s.get("density", 1.0), 1e-6)
            Rp = np.eye(3)
            if "capsule" in s:
                a, b, r = s["capsule"]
                L = float(np.linalg.norm(b - a))
                vol = math.pi * r * r * L + 4.0 / 3.0 * math.pi * r ** 3
                c = (a + b) / 2
                ext = (L / 2 + r, r, r)
                if L > 1e-9:
                    u = (b - a) / L
                    t = np.array([0.0, 0.0, 1.0]) if abs(u[2]) < 0.9 else np.array([1.0, 0.0, 0.0])
                    v = np.cross(u, t)
                    v /= np.linalg.norm(v)
                    Rp = np.column_stack([u, v, np.cross(u, v)])
            elif "sphere" in s:
                c, r = s["sphere"]
                vol = 4.0 / 3.0 * math.pi * r ** 3
                ext = (r, r, r)
            elif "box" in s:
                M, hx = s["box"]
                c = M[:3, 3]
                vol = 8 * abs(hx[0] * hx[1] * hx[2])
                ext = tuple(abs(hx))
                Rp = orthonormal(M[:3, :3])
            elif "polytope" in s:
                v = s["polytope"]
                lo, hi = v.min(0), v.max(0)
                c = (lo + hi) / 2
                e = (hi - lo) / 2
                vol = 8 * abs(e[0] * e[1] * e[2])
                ext = tuple(e)
            else:
                continue
            m = den * vol
            parts.append((m, c, ext, Rp))
            m_tot += m
            com += m * c
        if m_tot <= 0:
            m_tot, com, parts = 1.0, np.zeros(3), [(1.0, np.zeros(3), (0.05, 0.05, 0.05), np.eye(3))]
        com /= m_tot
        I = np.zeros((3, 3))
        for m, c, e, Rp in parts:
            a, b, cc = e
            Ib = np.diag(m / 3.0 * np.array([b * b + cc * cc, a * a + cc * cc, a * a + b * b]))
            d = c - com
            I += Rp @ Ib @ Rp.T + m * (np.dot(d, d) * np.eye(3) - np.outer(d, d))
        I = (I + I.T) / 2 + np.eye(3) * 1e-9 * m_tot
        self.mass = m_tot
        self.com_local = com
        self.I = I
        self.invm = 0.0 if kinematic else 1.0 / m_tot
        self.invI = np.zeros((3, 3)) if kinematic else np.linalg.inv(I)
        # state: x = world position of the BODY ORIGIN, q = orientation
        self.x = rec["pos"].astype(float).copy()
        self.q = np.array([1.0, 0, 0, 0])
        self.v = np.zeros(3)
        self.w = np.zeros(3)
        d = rec.get("drag", 0.0) or 0.0
        self.damp = d if d > 0 else DEFAULT_DAMP
        d = rec.get("angdrag", 0.0) or 0.0
        self.adamp = d if d > 0 else DEFAULT_DAMP
        self.gscale = rec.get("gravity", 1.0)       # BDY3+ 0x18 (physread)

    def com(self):
        return self.x + qrot(self.q, self.com_local)

    def world_invI(self, n):
        """n . I_world^-1 . n for a world vector n."""
        R = qmat(self.q)
        ln = R.T @ n
        return float(ln @ self.invI @ ln)

    def apply_inv_inertia(self, v):
        R = qmat(self.q)
        return R @ (self.invI @ (R.T @ v))


def _turn(b, dq):
    """Rotate a body by the small world rotation vector dq ABOUT ITS CENTRE OF
    MASS. The state keeps the body origin, so the origin is moved to keep the
    centre where it was - turning about the origin instead slid the centre
    sideways on every correction and pumped energy into hanging chains."""
    c = b.com()
    b.q = qnorm(b.q + 0.5 * qmul(np.array([0.0, *dq]), b.q))
    b.x = c - qrot(b.q, b.com_local)


def _apply_pos(b, p, r_world, sign):
    if b.kin:
        return
    b.x = b.x + sign * p * b.invm
    _turn(b, b.apply_inv_inertia(np.cross(r_world, sign * p)))


def positional(b1, b2, p1_local, p2_local, h2, compliance=0.0, max_dist=0.0):
    """Points p1 (on b1) and p2 (on b2), body-origin local, held together
    (or within max_dist)."""
    r1 = qrot(b1.q, p1_local - b1.com_local)
    r2 = qrot(b2.q, p2_local - b2.com_local)
    w1p = b1.com() + r1
    w2p = b2.com() + r2
    d = w1p - w2p
    c = float(np.linalg.norm(d))
    if c <= max_dist + 1e-9:
        return 0.0
    n = d / c
    c -= max_dist
    w1 = b1.invm + (b1.world_invI(np.cross(r1, n)) if not b1.kin else 0.0)
    w2 = b2.invm + (b2.world_invI(np.cross(r2, n)) if not b2.kin else 0.0)
    if w1 + w2 <= 0:
        return c
    dl = -c / (w1 + w2 + compliance / h2)
    p = dl * n
    _apply_pos(b1, p, r1, +1)
    _apply_pos(b2, p, r2, -1)
    return c


def angular(b1, b2, rotvec, h2, compliance=0.0, omega=0.0, max_dl=0.0):
    """Rotate the two bodies towards each other by the world rotation vector
    rotvec (applied +b1 / -b2). `omega` > 0 makes it a spring of that angular
    frequency over the pair's effective inertia (Domino/Box2D soft joints:
    k = omega^2 / w); `max_dl` > 0 caps the step at a motor's max torque (h^2 x
    torque)."""
    th = float(np.linalg.norm(rotvec))
    if th < 1e-9:
        return
    n = rotvec / th
    w1 = b1.world_invI(n) if not b1.kin else 0.0
    w2 = b2.world_invI(n) if not b2.kin else 0.0
    if w1 + w2 <= 0:
        return
    if omega > 0:
        compliance = (w1 + w2) / (omega * omega)
    dl = -th / (w1 + w2 + compliance / h2)
    if max_dl > 0:
        dl = max(-max_dl, min(max_dl, dl))
    p = dl * n
    if not b1.kin:
        _turn(b1, b1.apply_inv_inertia(p))
    if not b2.kin:
        _turn(b2, b2.apply_inv_inertia(-p))


def limit_angle(n, n1, n2, a, lo, hi):
    """XPBD's limitAngle: rotation vector that brings the angle of n1->n2
    about n into [lo, hi]."""
    phi = math.asin(max(-1.0, min(1.0, float(np.dot(np.cross(n1, n2), n)))))
    if np.dot(n1, n2) < 0:
        phi = math.pi - phi
    if phi > math.pi:
        phi -= 2 * math.pi
    if phi < -math.pi:
        phi += 2 * math.pi
    if lo <= phi <= hi:
        return None
    phi = max(lo, min(hi, phi))
    n1r = qrot(np.array([math.cos(phi / 2), *(math.sin(phi / 2) * n)]), n1)
    corr = np.cross(n1r, n2)
    return corr


class Joint:
    def __init__(self, rec, bodies):
        self.rec = rec
        self.t = rec["type"]
        self.b1 = bodies[rec["a"]]
        self.b2 = bodies[rec["b"]]
        if "frameA" in rec:
            self.fa = rec["frameA"]
            self.fb = rec["frameB"]
            self.pa = self.fa[:3, 3].copy()
            self.pb = self.fb[:3, 3].copy()
            self.ra = orthonormal(self.fa[:3, :3])
            self.rb = orthonormal(self.fb[:3, :3])
        else:
            self.pa = rec.get("anchorA", np.zeros(3))
            self.pb = rec.get("anchorB", np.zeros(3))
            self.ra = self.rb = np.eye(3)
        self.err = 0.0

    def axes(self):
        A = qmat(self.b1.q) @ self.ra
        Bm = qmat(self.b2.q) @ self.rb
        return A, Bm

    def motor(self, h2):
        """A position motor (mode 1) springs the joint back to its frames'
        alignment - the pose it was authored in - at the motor's frequency,
        never harder than its max torque. Soft: applied once per substep
        (single-iteration XPBD, so the compliance is exact)."""
        mo = self.rec.get("motor")
        if not mo or mo["mode"] != 1 or mo["max"] <= 0 or self.t not in (1, 3):
            return
        A, Bm = self.axes()
        omega = 2 * math.pi * mo["freq"] if mo["freq"] > 0 else 0.0
        if self.t == 1:
            dq = qmul(mat_q(A), qconj(mat_q(Bm)))
            if dq[0] < 0:
                dq = -dq
            rv = 2.0 * dq[1:]           # dq takes B to A: undo it (unit-tested)
        else:                                       # revolute: about the hinge only
            ax = A[:, 2]
            c = np.cross(A[:, 0], Bm[:, 0])
            rv = -ax * float(np.dot(c, ax))
        angular(self.b1, self.b2, rv, h2, omega=omega, max_dl=mo["max"] * h2)

    def solve(self, h2, first=True):
        t = self.t
        if t == 5:
            self.err = max(self.err, positional(self.b1, self.b2, self.pa, self.pb, h2, 0.0,
                                                max_dist=self.rec.get("dist", 0.0)))
            return
        if first:
            self.motor(h2)
        A, Bm = self.axes()
        if t == 2:                                  # weld: orientation held (soft)
            f = self.rec.get("freq", 0.0)
            if f <= 0 or first:
                dq = qmul(mat_q(A), qconj(mat_q(Bm)))
                if dq[0] < 0:
                    dq = -dq
                angular(self.b1, self.b2, 2.0 * dq[1:], h2, omega=2 * math.pi * f if f > 0 else 0.0)
        elif t == 3:                                # revolute: z axes together
            a1, a2 = A[:, 2], Bm[:, 2]
            angular(self.b1, self.b2, -np.cross(a1, a2), h2)
            lo, hi = self.rec.get("limits", (0.0, 0.0))
            A, Bm = self.axes()
            corr = limit_angle(A[:, 2], A[:, 0], Bm[:, 0], 0, math.radians(lo), math.radians(hi))
            if corr is not None:
                angular(self.b1, self.b2, -corr, h2)
        elif t == 1:                                # shoulder: cone + twist
            cone = self.rec.get("cone", math.pi)
            a1, a2 = A[:, 2], Bm[:, 2]
            ang = math.acos(max(-1.0, min(1.0, float(np.dot(a1, a2)))))
            if ang > cone:
                ax = np.cross(a1, a2)
                nrm = np.linalg.norm(ax)
                if nrm > 1e-9:
                    ax /= nrm
                    angular(self.b1, self.b2, -ax * (ang - cone), h2)
            lo, hi = self.rec.get("twist", (-180.0, 180.0))
            A, Bm = self.axes()
            nh = A[:, 2] + Bm[:, 2]
            nn = np.linalg.norm(nh)
            if nn > 1e-9:
                nh /= nn
                n1 = A[:, 0] - nh * np.dot(nh, A[:, 0])
                n2 = Bm[:, 0] - nh * np.dot(nh, Bm[:, 0])
                if np.linalg.norm(n1) > 1e-9 and np.linalg.norm(n2) > 1e-9:
                    corr = limit_angle(nh, n1 / np.linalg.norm(n1), n2 / np.linalg.norm(n2), 0,
                                       math.radians(lo), math.radians(hi))
                    if corr is not None:
                        angular(self.b1, self.b2, -corr, h2)
        elif t == 4:                                # prismatic: orientation held
            dq = qmul(mat_q(A), qconj(mat_q(Bm)))
            if dq[0] < 0:
                dq = -dq
            angular(self.b1, self.b2, 2.0 * dq[1:], h2)
        self.err = max(self.err, positional(self.b1, self.b2, self.pa, self.pb, h2, 0.0))



# ---------------------------------------------------------------------------
# contacts: the moving parts against the wearer's stand-in shapes
# ---------------------------------------------------------------------------
# A retail .phys carries its own stand-ins for the wearer: shapes on the
# KINEMATIC bodies (the bodies that follow the character's bones). Measured on
# 304 retail models (12.1.0.69933): 258 kinematic bodies carry shapes - 255
# capsules, 6 hulls - while retail character bodies carry no physics at all
# (no PFID/PFDC on the model, none in the .skel). So what a hood or a plume
# rests on is those shapes. The first offline solver had no contacts: every
# part fell straight through them and hung in free space (helms beside the
# head, in game 2026-10-09). Here every dynamic capsule (and every vertex of a
# dynamic hull) is kept outside every kinematic capsule, with static friction.
# Pairs that already overlap in the authored (bind) pose are left alone: the
# author put them there (a link's own anchor sits inside the proxy).
CONTACTS = True


def _seg_closest(p1, q1, p2, q2):
    """Closest points of segments p1q1 and p2q2 (Ericson, RTCD 5.1.9)."""
    d1, d2, r = q1 - p1, q2 - p2, p1 - p2
    a, e, f = float(d1 @ d1), float(d2 @ d2), float(d2 @ r)
    if a <= 1e-12 and e <= 1e-12:
        return p1, p2
    if a <= 1e-12:
        s, t = 0.0, min(max(f / e, 0.0), 1.0)
    else:
        c = float(d1 @ r)
        if e <= 1e-12:
            t, s = 0.0, min(max(-c / a, 0.0), 1.0)
        else:
            b = float(d1 @ d2)
            den = a * e - b * b
            s = min(max((b * f - c * e) / den, 0.0), 1.0) if den > 1e-12 else 0.0
            t = (b * s + f) / e
            if t < 0.0:
                t, s = 0.0, min(max(-c / a, 0.0), 1.0)
            elif t > 1.0:
                t, s = 1.0, min(max((b - c) / a, 0.0), 1.0)
    return p1 + d1 * s, p2 + d2 * t


def _parts(body, shapes):
    """[(kind, local a, local b, radius, friction)] - capsules as segments,
    hull vertices as zero-radius points, spheres as degenerate segments."""
    out = []
    for s in shapes:
        fr = float(s.get("friction", 0.2))
        if "capsule" in s:
            a, b, r = s["capsule"]
            out.append(("seg", np.asarray(a, float), np.asarray(b, float), float(r), fr))
        elif "sphere" in s:
            c, r = s["sphere"]
            out.append(("seg", np.asarray(c, float), np.asarray(c, float), float(r), fr))
        elif "polytope" in s:
            for v in np.asarray(s["polytope"], float):
                out.append(("pt", v, v, 0.0, fr))
    return out


class Contacts:
    def __init__(self, bodies, phys):
        self.pairs = []
        self.skipped_bind = 0
        self.kin_hulls = 0
        parts = []
        for b, rec in zip(bodies, phys["bodies"]):
            sh = phys["shapes"][rec["shape0"]:rec["shape0"] + rec["nshape"]] if rec["nshape"] > 0 else []
            parts.append(_parts(b, sh))
        for i, bd in enumerate(bodies):
            if bd.kin:
                continue
            for j, bk in enumerate(bodies):
                if not bk.kin:
                    continue
                for pd in parts[i]:
                    for pk in parts[j]:
                        if pk[0] == "pt":
                            self.kin_hulls += 1      # a kinematic hull: its vertices are no surface
                            continue
                        if self._depth(bd, pd, bk, pk)[0] > 1e-4:
                            self.skipped_bind += 1
                            continue
                        self.pairs.append((bd, pd, bk, pk))

    @staticmethod
    def _world(b, p):
        return b.x + qrot(b.q, p)

    def _depth(self, bd, pd, bk, pk):
        a1, b1 = self._world(bd, pd[1]), self._world(bd, pd[2])
        a2, b2 = self._world(bk, pk[1]), self._world(bk, pk[2])
        c1, c2 = _seg_closest(a1, b1, a2, b2)
        d = c1 - c2
        dist = float(np.linalg.norm(d))
        depth = pd[3] + pk[3] - dist
        n = d / dist if dist > 1e-9 else np.array([0.0, 0.0, 1.0])
        return depth, n, c1, c2

    def solve(self):
        worst = 0.0
        for bd, pd, bk, pk in self.pairs:
            depth, n, c1, _c2 = self._depth(bd, pd, bk, pk)
            if depth <= 0:
                continue
            worst = max(worst, depth)
            p_world = c1 - n * pd[3]                  # the dynamic surface point
            r = p_world - bd.com()
            w = bd.invm + bd.world_invI(np.cross(r, n))
            if w <= 0:
                continue
            _apply_pos(bd, (depth / w) * n, r, +1)
            # static friction (XPBD 3.5): cancel the tangential slide of this
            # contact point since the substep began while it is within mu*depth
            mu = math.sqrt(max(pd[4], 0.0) * max(pk[4], 0.0))
            if mu > 0 and hasattr(bd, "prev_x"):
                prev = bd.prev_x + qrot(bd.prev_q, qrot(qconj(bd.q), p_world - bd.x))
                dp = (p_world - prev)
                dt = dp - n * float(dp @ n)
                lt = float(np.linalg.norm(dt))
                if 1e-9 < lt < mu * depth:
                    r = p_world - bd.com()
                    tn = dt / lt
                    w = bd.invm + bd.world_invI(np.cross(r, tn))
                    if w > 0:
                        _apply_pos(bd, -(lt / w) * tn, r, +1)
        return worst

# ---------------------------------------------------------------------------
def simulate(phys: dict, root_motion, duration: float, warmup: int = WARMUP_LOOPS,
             stop_still: float = 0.0):
    """root_motion(t) -> 4x4 world transform of the model at time t (the
    attachment point it hangs on, with the model's own root animation).
    `stop_still` > 0 ends the run once every body has moved less than that per
    frame for two whole seconds (nothing left to settle; the frames kept are
    what the full run would have repeated).
    Returns ({bone: [(R, x_origin) per frame in MODEL space]}, max joint error)."""
    bodies = []
    for i, b in enumerate(phys["bodies"]):
        shapes = phys["shapes"][b["shape0"]:b["shape0"] + b["nshape"]] if b["nshape"] > 0 else []
        bodies.append(Body(b, shapes, kinematic=(b["type"] != 1)))
    joints = [Joint(j, bodies) for j in phys["joints"] if j["a"] < len(bodies) and j["b"] < len(bodies)]
    bind = [(b.x.copy(), b.q.copy()) for b in bodies]
    contacts = Contacts(bodies, phys) if CONTACTS else None
    h = 1.0 / FPS / SUBSTEPS
    nframes = max(2, int(round(duration * FPS)))
    total = nframes * (warmup + 1)
    out = {b.bone: [] for b in bodies if not b.kin}
    max_err = 0.0
    M0 = root_motion(0.0)
    for b, (x0, q0) in zip(bodies, bind):           # start where the model starts
        b.x = M0[:3, :3] @ x0 + M0[:3, 3]
        b.q = qnorm(qmul(mat_q(M0[:3, :3]), q0))
    for f in range(total):
        for s in range(SUBSTEPS):
            t = ((f + s / SUBSTEPS) % nframes) / FPS
            M = root_motion(t)
            Mq = mat_q(M[:3, :3])
            for b, (x0, q0) in zip(bodies, bind):
                if b.kin:
                    b.x = M[:3, :3] @ x0 + M[:3, 3]
                    b.q = qnorm(qmul(Mq, q0))
                    continue
                b.prev_x, b.prev_q = b.x.copy(), b.q.copy()
                b.v = b.v + h * G * b.gscale
                # gyroscopic term (XPBD, Mueller 2020 eq. 7): without it a body
                # with an asymmetric tensor keeps w fixed while its inertia
                # turns, and rotational energy grows until the chain flails
                Rb = qmat(b.q)
                Iw = Rb @ b.I @ Rb.T
                b.w = b.w - h * (Rb @ (b.invI @ (Rb.T @ np.cross(b.w, Iw @ b.w))))
                b.v *= max(0.0, 1.0 - h * b.damp)
                b.w *= max(0.0, 1.0 - h * b.adamp)
                c = b.com() + h * b.v
                b.q = qnorm(b.q + 0.5 * h * qmul(np.array([0.0, *b.w]), b.q))
                b.x = c - qrot(b.q, b.com_local)
            for j in joints:
                j.err = 0.0
            for it in range(ITERATIONS):
                for j in joints:
                    j.solve(h * h, it == 0)
                if contacts is not None:
                    contacts.solve()
            for b in bodies:
                if b.kin:
                    continue
                pc = b.prev_x + qrot(b.prev_q, b.com_local)
                b.v = (b.com() - pc) / h
                dq = qmul(b.q, qconj(b.prev_q))
                b.w = 2.0 * dq[1:] / h if dq[0] >= 0 else -2.0 * dq[1:] / h
            if f >= nframes * warmup:
                max_err = max(max_err, max((j.err for j in joints), default=0.0))
        if f >= nframes * warmup:
            t = (f % nframes) / FPS
            Mi = np.linalg.inv(root_motion(t))
            for b, (x0, _q0) in zip(bodies, bind):
                if b.kin:
                    continue
                R = Mi[:3, :3] @ qmat(b.q)
                x = Mi[:3, :3] @ b.x + Mi[:3, 3]
                out[b.bone].append((R, x))
        if not np.isfinite(sum(float(np.sum(b.x)) for b in bodies)):
            raise RuntimeError("simulation diverged")
        if stop_still > 0 and out and f >= nframes * warmup:
            k = len(next(iter(out.values())))
            if k > 2 * FPS and k % FPS == 0 and all(
                    _moved(v[i], v[i - 1]) < stop_still
                    for v in out.values() for i in range(k - 2 * FPS, k)):
                break
    return out, max_err


# ---------------------------------------------------------------------------
# writing the keys
# ---------------------------------------------------------------------------
def _q16(v: float) -> int:
    """3.3.5 compressed quaternion component, the inverse of
    f = (s < 0 ? s + 32768 : s - 32767) / 32767 (m2_view.q16)."""
    v = max(-1.0, min(1.0, v))
    if v >= 0:
        return int(round(v * 32767 - 32768)) if v < 1.0 else -1
    return int(round(v * 32767 + 32767))


def bake_into(model: bytes, phys: dict, frames: dict, duration_ms: int) -> bytes:
    """Write rotation + translation keys for every dynamic body's bone on
    sequence 0 (resident), and the animated flag 0x200 on those bones and
    their descendants."""
    import m2edit as E
    m = bytearray(model)
    nb, ob = struct.unpack_from("<II", m, 0x2C)
    ns = struct.unpack_from("<I", m, 0x1C)[0]
    parents = [struct.unpack_from("<h", m, ob + i * 88 + 8)[0] for i in range(nb)]
    pivots = [np.array(struct.unpack_from("<3f", m, ob + i * 88 + 76)) for i in range(nb)]
    bodies_of = {b["bone"]: b for b in phys["bodies"]}

    def world_of(bone, k):
        """Model-space transform of `bone` at frame k: a dynamic body's own,
        else its parent's (static bones are identity at rest)."""
        T = np.eye(4)
        if bone in frames:
            R, x = frames[bone][k]
            x0 = bodies_of[bone]["pos"]
            T[:3, :3] = R
            T[:3, 3] = x - R @ x0
            return T
        p = parents[bone]
        return world_of(p, k) if p >= 0 else T

    def append(data: bytes, align=16):
        while len(m) % align:
            m.append(0)
        at = len(m)
        m.extend(data)
        return at

    n = len(next(iter(frames.values())))
    times = [int(round(i * duration_ms / max(1, n - 1))) for i in range(n)] if n > 1 else [0]
    animated = set()
    for bone in frames:
        if bone >= nb:
            continue
        p = parents[bone]
        rots, trs = [], []
        piv = pivots[bone]
        for k in range(n):
            W = world_of(bone, k)
            P = world_of(p, k) if p >= 0 else np.eye(4)
            L = np.linalg.inv(P) @ W
            R = L[:3, :3]
            tr = L[:3, 3] - piv + R @ piv
            q = mat_q(R)
            rots.append(q)
            trs.append(tr)
        # the loop closes: last key = first key
        rots[-1], trs[-1] = rots[0], trs[0]
        for i in range(1, len(rots)):
            if np.dot(rots[i], rots[i - 1]) < 0:
                rots[i] = -rots[i]
        t_blob = struct.pack("<%dI" % n, *times)
        r_blob = b"".join(struct.pack("<4h", _q16(q[1]), _q16(q[2]), _q16(q[3]), _q16(q[0])) for q in rots)
        x_blob = b"".join(struct.pack("<3f", *x) for x in trs)
        for off, blob in ((0x10, x_blob), (0x24, r_blob)):
            tr = ob + bone * 88 + off
            struct.pack_into("<hh", m, tr, 1, -1)            # linear, no global sequence
            cnt_t, o_t = struct.unpack_from("<II", m, tr + 4)
            cnt_v, o_v = struct.unpack_from("<II", m, tr + 12)
            if cnt_t < ns or cnt_v < ns:                     # per-sequence arrays rebuilt
                o_t = append(bytes(8 * ns))
                o_v = append(bytes(8 * ns))
                struct.pack_into("<II", m, tr + 4, ns, o_t)
                struct.pack_into("<II", m, tr + 12, ns, o_v)
            at_t = append(t_blob)
            at_v = append(blob)
            struct.pack_into("<II", m, o_t, n, at_t)
            struct.pack_into("<II", m, o_v, n, at_v)
        animated.add(bone)
    changed = True
    while changed:
        changed = False
        for i in range(nb):
            if i not in animated and parents[i] in animated:
                animated.add(i)
                changed = True
    for i in animated:
        fl = struct.unpack_from("<I", m, ob + i * 88 + 4)[0]
        struct.pack_into("<I", m, ob + i * 88 + 4, fl | 0x200)
    out, _ = E.union_bounds(bytes(m), margin=0.3)
    return out


def static_root(_t):
    return np.eye(4)


def _probe(frame):
    """A body's origin and three points 0.1 yd along its axes: a link turning
    about its own origin (its joint) moves these even when the origin stays."""
    R, x = frame
    return np.vstack([x, x + 0.1 * R.T])


def _moved(a, b) -> float:
    return float(np.max(np.linalg.norm(_probe(a) - _probe(b), axis=1)))


def settle(phys: dict, max_seconds: float = 30.0, still: float = 1e-4):
    """Simulate with the model at rest until every dynamic body has stopped
    (per-frame displacement below `still` yd for one second): the pose
    retail's physics hangs the item in while its wearer stands still.
    Returns ({bone: (R, x_origin)}, max joint error at rest, seconds)."""
    frames, err = simulate(phys, static_root, max_seconds, warmup=0, stop_still=1e-5)
    n = len(next(iter(frames.values())))
    motion = [0.0] + [max(_moved(v[k], v[k - 1]) for v in frames.values()) for k in range(1, n)]
    # when it first came to rest (for the report)
    rest_at = max_seconds
    for k in range(1, n):
        if motion[k] < still:
            rest_at = k / FPS
            break
    # the window baked: the STILLEST second after the first two (the last one
    # when the item comes fully to rest). A chain whose tip floats over links
    # that hang has no preferred side: once settled it wanders sideways a few
    # mm/s for as long as it is simulated, so the end of a run is an arbitrary
    # point of that wander while its stillest second is the rest it reached.
    best_end, worst = n, float("inf")
    for end in range(min(n, 3 * FPS), n + 1, FPS // 2):
        w = max(motion[end - FPS:end])
        if w <= worst:
            best_end, worst = end, w
    tail = range(best_end - FPS, best_end)
    prev = range(max(1, best_end - 2 * FPS), max(2, best_end - FPS))
    # settled = the one-second mean no longer moves: a chain resting on a
    # joint limit keeps a few millimetres of solver chatter, and a pendulum
    # still swinging slowly is centred on its equilibrium - the mean is the
    # rest pose in both cases, and that is what is baked
    drift = max(float(np.max(np.linalg.norm(np.mean([_probe(v[k]) for k in tail], 0)
                                            - np.mean([_probe(v[k]) for k in prev], 0), axis=1)))
                for v in frames.values())
    pose = {}
    for b, v in frames.items():
        xs = np.mean([v[k][1] for k in tail], 0)
        q0 = mat_q(v[tail[-1]][0])
        acc = np.zeros(4)
        for k in tail:
            q = mat_q(v[k][0])
            acc += q if np.dot(q, q0) >= 0 else -q
        pose[b] = (qmat(qnorm(acc)), xs)
    return pose, err, rest_at, worst, drift


# bake_model / main (the Midnight pipeline's driver) are not part of the kit:
# shipping a baked pose is OFF (see references/rules.md "Physics"). The kit
# uses simulate() / settle() for study only: python bp.py phys --settle.


if __name__ == "__main__":
    raise SystemExit("library module: run `python bp.py phys --work W <fdid> --settle`")
