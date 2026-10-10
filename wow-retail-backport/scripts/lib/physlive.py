r"""
physlive.py - retail runtime physics (.phys) stepped frame by frame from the
wearer's real motion: the REFERENCE a client mod's solver is held to, value
for value (references/client-extension.md section 5; rules.md section 6).

    m = Model(physread.parse(blob), shift)
    out = m.step({bone: 4x4 world matrix of that bone}, dt)   # every frame
          -> {dynamic bone: 4x4 world matrix}                  # what to draw

A bone matrix is column form and maps a MODEL-BIND point to the world. A mod
builds it from the client's bone matrix (camera-relative in 3.3.5a) and the
scene's camera-to-world matrix, so the camera never moves anything. The model
may be scaled (s = length of the root bone matrix's first column): the
simulation runs in world / s, where every shape keeps its authored size and
gravity is 10 / s.

ONE SUBSTEP (h = 1/120 s; fixed, so the result does not depend on frame rate)
  kinematic bodies: their bone pose, interpolated over the frame
  dynamic bodies: gravity x signed scale, gyroscopic term, damping, predict
  ITERATIONS passes: every joint (physbake.Joint: motors and soft welds in
  the first pass), then every contact
  velocities from the positions
CONTACTS each dynamic body's capsules against the kinematic bodies' capsules
  (a hull = its bounding capsule): the dynamic one is pushed out to touch.
  Bodies a joint connects never collide (retail authors overlaps there).
RESET to the client's own bone poses (never an explosion): first frame, a gap
  over GAP, a root jump over JUMP yd, a scale change, a non-finite state.
"""
from __future__ import annotations

import math

import numpy as np

import physbake as PB

H = 1.0 / 120
ITERATIONS = 2
MAX_SUBSTEPS = 6
GAP = 0.25           # s: a longer frame (loading, alt-tab, off screen) resets
JUMP = 4.0           # yd: a root moving further in one frame teleported
G = np.array([0.0, 0.0, -10.0])


def closest_seg_seg(p1, q1, p2, q2):
    """Closest points (c1 on p1q1, c2 on p2q2): Ericson, Real-Time Collision
    Detection 5.1.9."""
    d1, d2, r = q1 - p1, q2 - p2, p1 - p2
    a, e, f = float(d1 @ d1), float(d2 @ d2), float(d2 @ r)
    eps = 1e-12
    if a <= eps and e <= eps:
        return p1.copy(), p2.copy()
    if a <= eps:
        s, t = 0.0, min(max(f / e, 0.0), 1.0)
    else:
        c = float(d1 @ r)
        if e <= eps:
            t, s = 0.0, min(max(-c / a, 0.0), 1.0)
        else:
            b = float(d1 @ d2)
            den = a * e - b * b
            s = min(max((b * f - c * e) / den, 0.0), 1.0) if den > eps else 0.0
            t = (b * s + f) / e
            if t < 0.0:
                t, s = 0.0, min(max(-c / a, 0.0), 1.0)
            elif t > 1.0:
                t, s = 1.0, min(max((b - c) / a, 0.0), 1.0)
    return p1 + d1 * s, p2 + d2 * t


def seg_seg_distance(p1, q1, p2, q2) -> float:
    c1, c2 = closest_seg_seg(np.asarray(p1, float), np.asarray(q1, float),
                             np.asarray(p2, float), np.asarray(q2, float))
    return float(np.linalg.norm(c1 - c2))


def capsules_of(shapes) -> list:
    """[(a, b, r)] body-local: capsules as authored, a sphere as a zero-length
    capsule, a box or hull as its bounding capsule along its longest axis."""
    out = []
    for s in shapes:
        if "capsule" in s:
            a, b, r = s["capsule"]
            out.append((np.asarray(a, float), np.asarray(b, float), float(r)))
        elif "sphere" in s:
            c, r = s["sphere"]
            out.append((np.asarray(c, float), np.asarray(c, float), float(r)))
        elif "polytope" in s or "box" in s:
            if "polytope" in s:
                v = np.asarray(s["polytope"], float)
            else:
                M, hx = s["box"]
                corners = np.array([[sx, sy, sz] for sx in (-1, 1) for sy in (-1, 1) for sz in (-1, 1)]) * np.abs(hx)
                v = corners @ PB.orthonormal(M[:3, :3]).T + M[:3, 3]
            c = v.mean(0)
            _u, _s, vt = np.linalg.svd(v - c)
            ax = vt[0]
            t = (v - c) @ ax
            rad = float(np.max(np.linalg.norm((v - c) - np.outer(t, ax), axis=1)))
            lo, hi = float(t.min()) + rad, float(t.max()) - rad
            if lo > hi:
                lo = hi = (lo + hi) / 2
            out.append((c + ax * lo, c + ax * hi, rad))
    return out


def _pose_of(M, bind, s):
    """(x, q) in simulation space of a body whose bind origin is `bind`, on a
    bone matrix M (column form, model-bind -> world, scaled by s)."""
    R3 = PB.orthonormal(M[:3, :3] / s)
    x = (M[:3, :3] @ bind + M[:3, 3]) / s
    return x, PB.mat_q(R3)


def _slerp(a, b, t):
    if float(a @ b) < 0:
        b = -b
    d = float(a @ b)
    if d > 0.9995:
        return PB.qnorm(a + t * (b - a))
    th = math.acos(min(1.0, d))
    return (math.sin((1 - t) * th) * a + math.sin(t * th) * b) / math.sin(th)


class Model:
    def __init__(self, phys: dict, shift=(0.0, 0.0, 0.0)):
        shift = np.asarray(shift, float)
        self.bodies, self.caps = [], []
        for b in phys["bodies"]:
            shapes = phys["shapes"][b["shape0"]:b["shape0"] + b["nshape"]] if b["nshape"] > 0 else []
            rec = dict(b)
            rec["pos"] = np.asarray(b["pos"], float) + shift
            self.bodies.append(PB.Body(rec, shapes, kinematic=(b["type"] != 1)))
            self.caps.append(capsules_of(shapes))
        n = len(self.bodies)
        self.joints = [PB.Joint(j, self.bodies) for j in phys["joints"] if j["a"] < n and j["b"] < n]
        self.bind = [b.rec["pos"].copy() for b in self.bodies]
        # bodies a joint connects never collide (Box2D collideConnected =
        # false; retail authors a plume's first link inside its helm's shell)
        self.joined = {(min(j["a"], j["b"]), max(j["a"], j["b"])) for j in phys["joints"]}
        kin = [i for i, b in enumerate(self.bodies) if b.kin]
        self.root = kin[0] if kin else 0
        self.live = False
        self.acc = 0.0
        self.scale = 1.0
        self.prev = None

    def bones(self) -> list:
        return sorted({b.bone for b in self.bodies})

    # -- state ---------------------------------------------------------------
    def _targets(self, kin, s):
        return [_pose_of(kin[b.bone], self.bind[i], s) if b.bone in kin else (b.x.copy(), b.q.copy())
                for i, b in enumerate(self.bodies)]

    def reset(self, kin, s):
        for b, (x, q) in zip(self.bodies, self._targets(kin, s)):
            b.x, b.q = x, q
            b.v, b.w = np.zeros(3), np.zeros(3)
        self.prev = [(b.x.copy(), b.q.copy()) for b in self.bodies]
        self.scale, self.acc, self.live = s, 0.0, True

    def _out(self, s):
        out = {}
        for i, b in enumerate(self.bodies):
            if b.kin:
                continue
            R3 = PB.qmat(b.q)
            M = np.eye(4)
            M[:3, :3] = s * R3
            M[:3, 3] = s * (b.x - R3 @ self.bind[i])
            out[b.bone] = M
        return out

    # -- one frame ---------------------------------------------------------
    def step(self, kin: dict, dt: float) -> dict:
        root = kin.get(self.bodies[self.root].bone)
        if root is None:
            root = next(iter(kin.values()))
        s = float(np.linalg.norm(root[:3, 0]))
        cur = self._targets(kin, s)
        if (not self.live or dt > GAP or dt < 0 or abs(s - self.scale) > 1e-4 * max(s, 1e-6)
                or np.linalg.norm(cur[self.root][0] - self.prev[self.root][0]) * s > JUMP):
            self.reset(kin, s)
            return self._out(s)
        self.acc += dt
        n = int(self.acc / H + 1e-9)
        if n > MAX_SUBSTEPS:
            n, self.acc = MAX_SUBSTEPS, 0.0
        else:
            self.acc -= n * H
        g = G / s
        h2 = H * H
        for k in range(n):
            t = (k + 1) / n
            for i, b in enumerate(self.bodies):
                if b.kin:
                    (x0, q0), (x1, q1) = self.prev[i], cur[i]
                    b.x = x0 + (x1 - x0) * t
                    b.q = PB.qnorm(_slerp(q0, q1, t))
                    continue
                b.prev_x, b.prev_q = b.x.copy(), b.q.copy()
                b.v = b.v + H * g * b.gscale
                Rb = PB.qmat(b.q)
                Iw = Rb @ b.I @ Rb.T
                b.w = b.w - H * (Rb @ (b.invI @ (Rb.T @ np.cross(b.w, Iw @ b.w))))
                b.v = b.v * max(0.0, 1.0 - H * b.damp)
                b.w = b.w * max(0.0, 1.0 - H * b.adamp)
                c = b.com() + H * b.v
                b.q = PB.qnorm(b.q + 0.5 * H * PB.qmul(np.array([0.0, *b.w]), b.q))
                b.x = c - PB.qrot(b.q, b.com_local)
            for it in range(ITERATIONS):
                for j in self.joints:
                    j.solve(h2, it == 0)
                self._contacts()
            for b in self.bodies:
                if b.kin:
                    continue
                pc = b.prev_x + PB.qrot(b.prev_q, b.com_local)
                b.v = (b.com() - pc) / H
                dq = PB.qmul(b.q, PB.qconj(b.prev_q))
                b.w = 2.0 * dq[1:] / H if dq[0] >= 0 else -2.0 * dq[1:] / H
        self.prev = cur
        if not all(np.isfinite(b.x).all() and np.isfinite(b.q).all() for b in self.bodies):
            self.reset(kin, s)
        return self._out(s)

    def _contacts(self):
        for i, d in enumerate(self.bodies):
            if d.kin or not self.caps[i]:
                continue
            for j, k in enumerate(self.bodies):
                if not k.kin or not self.caps[j] or (min(i, j), max(i, j)) in self.joined:
                    continue
                for a1, b1, r1 in self.caps[i]:
                    for a2, b2, r2 in self.caps[j]:
                        P1 = d.x + PB.qrot(d.q, a1)
                        Q1 = d.x + PB.qrot(d.q, b1)
                        P2 = k.x + PB.qrot(k.q, a2)
                        Q2 = k.x + PB.qrot(k.q, b2)
                        c1, c2 = closest_seg_seg(P1, Q1, P2, Q2)
                        dv = c1 - c2
                        dist = float(np.linalg.norm(dv))
                        depth = r1 + r2 - dist
                        if depth <= 0:
                            continue
                        if dist > 1e-9:
                            nrm = dv / dist
                        else:
                            nrm = d.com() - k.com()
                            ln = float(np.linalg.norm(nrm))
                            nrm = nrm / ln if ln > 1e-9 else np.array([0.0, 0.0, 1.0])
                        rw = (c1 - nrm * r1) - d.com()
                        w = d.invm + d.world_invI(np.cross(rw, nrm))
                        if w <= 0:
                            continue
                        PB._apply_pos(d, (depth / w) * nrm, rw, +1)
