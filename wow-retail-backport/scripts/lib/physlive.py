r"""
physlive.py - retail runtime physics (.phys) stepped frame by frame from the
wearer's real motion: the REFERENCE a client mod's solver is held to, value
for value (references/client-extension.md section 5; rules.md section 6).

    m = Model(physread.parse(blob), shift)
    out = m.step({bone: 4x4 world matrix of that bone}, dt)   # every frame
          -> {dynamic bone: 4x4 world matrix}                  # what to draw

A bone matrix is column form and maps a MODEL-BIND point to the world (a mod
builds it from the client's camera-relative bone matrix and the scene's
camera-to-world matrix, so the camera never moves anything). The model may be
scaled (s = length of the root bone matrix's first column): the simulation
runs in world / s, where every shape keeps its authored size and gravity is
10 / s.

ONE SUBSTEP (h = 1/240 s; fixed, so the result does not depend on frame rate)
  kinematic bodies: their bone pose, interpolated over the frame
  dynamic bodies: gravity x signed scale, gyroscopic term, damping, predict
  ITERATIONS passes: every joint (physbake.Joint: motors and soft
  welds in the first pass), then every contact
  velocities from the positions
CONTACTS each dynamic body's capsules against the kinematic bodies' capsules
  (a hull = its bounding capsule): the dynamic one is pushed out to touch.
  Bodies a joint connects never collide (retail authors overlaps there).
RESET to the client's own bone poses (never an explosion): first frame, a gap
  over GAP, a root jump over JUMP yd, a scale change, a non-finite state.
THE WEARER'S TRAVEL (run, jump, turn, fly) is not wind: every frame the
  dynamic bodies are first carried rigidly with the root (positions,
  orientations, velocities), so the solver works in the wearer's own frame -
  steady travel and steady turning move nothing, damping slows motion RELATIVE
  to the wearer (in the world it dragged a tassel 70 deg back at a run and
  flung it over at the start). A CHANGE of the root's velocity is felt as
  inertia, bounded the way game physics bounds it (Unreal AnimDynamics
  inertia limits, Havok's local-space ragdoll): at most INERTIA_SPEED of any
  velocity change within VEL_WINDOW, released at INERTIA_ACCEL (frame-rate
  independent) - so a sustained one (a jump arc, a turn's centripetal pull)
  is felt up to INERTIA_SPEED / VEL_WINDOW = 2.5 yd/s^2: a jump lightens an
  item by a third, never floats it; the same for spin (INERTIA_SPIN,
  INERTIA_SPIN_ACCEL). WoW's travel changes are instant (0 -> 7 yd/s in a
  frame, a 19.3 yd/s^2 jump arc, mouse snaps): felt whole they threw items
  over their joints. The travel is MEASURED over VEL_WINDOW with a hysteresis
  along it (VEL_STEP): the game moves the wearer by its own frame time, and a
  1 ms disagreement with ours, felt, shook a running tassel 17 deg.
SLEEP (as Box2D / PhysX / Havok sleep, judged by position): a model whose
  every body point stays inside a SLEEP_BOX box (relative to its root) for
  SLEEP_TIME stops being stepped and holds its pose on the root exactly - no
  solver flicker, no cost. A real swing spans its whole amplitude and never
  sleeps (a speed threshold froze a 2.7 mm swing mid-air); what can freeze is
  at most SLEEP_BOX / 2 off its rest (1.5 mm, about a pixel 1 m from the camera;
  Box2D tolerates 5 mm). It wakes on what it would feel: an inertia kick over
  WAKE_SPEED (or WAKE_TURN), the root tilting against gravity faster than
  WAKE_TURN, or another kinematic body moving on the root faster than
  WAKE_SPEED - steady running asleep costs nothing.
"""
from __future__ import annotations

import math

import numpy as np

import physbake as PB

H = 1.0 / 240
ITERATIONS = 4
MAX_SUBSTEPS = 12
GAP = 0.25           # s: a longer frame (loading, alt-tab, off screen) resets
JUMP = 4.0           # yd: a root moving further in one frame teleported
G = np.array([0.0, 0.0, -10.0])
SLEEP_TIME = 0.5               # s at rest before sleeping (Box2D b2_timeToSleep)
SLEEP_BOX = 0.003              # model units over SLEEP_TIME: under Box2D (1 cm/s x 0.5 s = 5 mm)
WAKE_SPEED = 0.05              # model units / s of any kinematic body
WAKE_TURN = math.radians(3.0)  # rad / s of any kinematic body
INERTIA_SPEED = 0.5            # yd/s: the most of one change of the wearer's velocity an item feels
INERTIA_ACCEL = 5.0            # yd/s^2 it is released at (a sustained change: INERTIA_SPEED / VEL_WINDOW)
INERTIA_SPIN = 2.0             # rad/s: the same for the wearer's spin
INERTIA_SPIN_ACCEL = 20.0      # rad/s^2
VEL_WINDOW = 0.2               # s: the root's velocity / spin are measured over this much
                               # time (one frame's: a 1 ms clock disagreement with the game
                               # shook a running tassel 17 deg)
VEL_STEP = 0.1                 # yd/s + VEL_STEP_REL x speed: a change ALONG the travel direction
VEL_STEP_REL = 0.03            # is felt only once it is this big (hysteresis). A clock
SPIN_STEP = 0.1                # disagreement only rescales the measured travel (never turns it):
SPIN_STEP_REL = 0.03           # noise around a steady run never moves the felt velocity, while
                               # turns, jumps and the head bob (across the travel) pass smoothly


def _clamp_len(v, n):
    ln = float(np.linalg.norm(v))
    return v * (n / ln) if ln > n else v


def _plain_sum(xs) -> float:
    """Left to right, as a C++ port adds (Python 3.12+ sum() compensates: six
    1/30 s frames sum to 0.2 there and to 0.19999999999999998 here, and the
    window's edge decision went the other way)."""
    t = 0.0
    for x in xs:
        t += x
    return t


def _felt_change(v, ref, step):
    """The change from the felt `ref` to the measured `v` that is felt now: its
    turn whole (ref swung onto v's direction, its length kept), its change of
    length only once that exceeds `step`. Continuous in the turn (a branch at
    90 deg made a run-into-jump a coin toss on rounding)."""
    n, r = float(np.linalg.norm(v)), float(np.linalg.norm(ref))
    if n < 1e-9 or r < 1e-9:
        d = v - ref
        return d if float(np.linalg.norm(d)) > step else np.zeros(3)
    swung = v * (r / n)
    turn = swung - ref
    return turn + (v - swung if abs(n - r) > step else np.zeros(3))


def _rotvec(q):
    """Rotation vector (axis x angle) of unit quaternion q (w, x, y, z)."""
    if q[0] < 0:
        q = -q
    s = float(np.linalg.norm(q[1:]))
    if s < 1e-12:
        return 2.0 * q[1:]
    return q[1:] * (2.0 * math.atan2(s, float(q[0])) / s)


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
        self.asleep = False
        self.rel = None
        self._wake()

    def _wake(self):
        self.asleep = False
        self.still, self.lo, self.hi = 0.0, None, None

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
        # the root's travel velocity / spin and what of their changes is still
        # to be felt; `fresh`: the next frame only measures (a model reset
        # while its wearer runs starts running, it is not kicked)
        self.vr, self.wr = np.zeros(3), np.zeros(3)
        self.pv, self.pw = np.zeros(3), np.zeros(3)
        self.fresh = True
        self.hist = []                  # (dt, root displacement, root rotation vector) per frame
        self._wake()

    # -- sleep ---------------------------------------------------------------
    def _rel(self, b):
        """Body b's (x, q) in its root body's frame."""
        r = self.bodies[self.root]
        qc = PB.qconj(r.q)
        return PB.qrot(qc, b.x - r.x), PB.qnorm(PB.qmul(qc, b.q))

    def _probes(self):
        out = []
        for b in self.bodies:
            if b.kin:
                continue
            x, q = self._rel(b)
            out += [x, x + PB.qrot(q, b.com_local * 2)]
        return np.array(out)

    def _watch(self, dt):
        """After a stepped frame: fall asleep once provably at rest - every
        body point inside a SLEEP_BOX box for SLEEP_TIME."""
        pr = self._probes()
        if self.lo is None:
            self.lo, self.hi, self.still = pr.copy(), pr.copy(), 0.0
            return
        lo, hi = np.minimum(self.lo, pr), np.maximum(self.hi, pr)
        if float(np.max(np.linalg.norm(hi - lo, axis=1))) > SLEEP_BOX:
            self.lo, self.hi, self.still = pr.copy(), pr.copy(), 0.0     # moving: the window restarts here
            return
        self.lo, self.hi = lo, hi
        self.still += dt
        if self.still >= SLEEP_TIME:
            self.rel = [self._rel(b) for b in self.bodies]
            for b in self.bodies:
                b.v, b.w = np.zeros(3), np.zeros(3)
            self.asleep = True

    def _woken(self, carried, cur, dt, fv, fw, s) -> bool:
        """What a sleeping model would feel this frame (`carried`: the previous
        kinematic poses already carried with the root onto `cur`)."""
        if dt <= 0:
            return False
        # the whole push still to come, not this frame's share of it (at 144 fps
        # a run start's share is under WAKE_SPEED: the push was lost asleep)
        if float(np.linalg.norm(fv + self.pv)) * s > WAKE_SPEED or float(np.linalg.norm(fw + self.pw)) > WAKE_TURN:
            return True
        (_x0, q0), (_x1, q1) = self.prev[self.root], cur[self.root]
        up0 = PB.qrot(PB.qconj(q0), np.array([0.0, 0.0, 1.0]))     # gravity as the root sees it
        up1 = PB.qrot(PB.qconj(q1), np.array([0.0, 0.0, 1.0]))
        if math.acos(max(-1.0, min(1.0, float(up0 @ up1)))) > WAKE_TURN * dt:
            return True
        for i, b in enumerate(self.bodies):
            if not b.kin or i == self.root:
                continue
            (x0, q0), (x1, q1) = carried[i], cur[i]
            dq = PB.qmul(q1, PB.qconj(q0))
            ang = 2.0 * math.acos(min(1.0, abs(float(dq[0]))))
            if np.linalg.norm(x1 - x0) > WAKE_SPEED * dt or ang > WAKE_TURN * dt:
                return True
        return False

    def _travel(self, cur, dt, s):
        """The root's rigid motion this frame: (carry(x, q) -> (x, q), its
        rotation dq, the felt velocity kick fv and spin kick fw)."""
        (x0, q0), (x1, q1) = self.prev[self.root], cur[self.root]
        dq = PB.qnorm(PB.qmul(q1, PB.qconj(q0)))

        def carry(x, q):
            return x1 + PB.qrot(dq, x - x0), PB.qnorm(PB.qmul(dq, q))
        if dt <= 0:
            return carry, dq, np.zeros(3), np.zeros(3)
        # [dt, displacement, rotation vector, felt kick, felt spin] per frame
        self.hist.append([dt, x1 - x0, _rotvec(dq), np.zeros(3), np.zeros(3)])
        while len(self.hist) > 1 and _plain_sum(h[0] for h in self.hist[1:]) >= VEL_WINDOW:
            self.hist.pop(0)
        span = _plain_sum(h[0] for h in self.hist)
        v = sum((h[1] for h in self.hist), np.zeros(3)) / span
        w = sum((h[2] for h in self.hist), np.zeros(3)) / span
        if self.fresh:
            self.vr, self.wr, self.fresh = v, w, False
        dv = _felt_change(v, self.vr, (VEL_STEP / s) + VEL_STEP_REL * float(np.linalg.norm(v)))
        dw = _felt_change(w, self.wr, SPIN_STEP + SPIN_STEP_REL * float(np.linalg.norm(w)))
        self.vr, self.wr = self.vr + dv, self.wr + dw
        # the window spreads one change over its frames: what was felt inside
        # it counts against the cap, so a change is felt once, whole or capped
        rv = sum((h[3] for h in self.hist), np.zeros(3))
        rw = sum((h[4] for h in self.hist), np.zeros(3))
        self.pv = _clamp_len(rv + self.pv + dv, INERTIA_SPEED / s) - rv
        self.pw = _clamp_len(rw + self.pw + dw, INERTIA_SPIN) - rw
        fv = _clamp_len(self.pv, INERTIA_ACCEL / s * dt)
        fw = _clamp_len(self.pw, INERTIA_SPIN_ACCEL * dt)
        self.pv, self.pw = self.pv - fv, self.pw - fw
        self.hist[-1][3], self.hist[-1][4] = fv, fw
        return carry, dq, fv, fw

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
        carry, dq, fv, fw = self._travel(cur, dt, s)
        carried = [carry(x, q) for x, q in self.prev]
        if self.asleep:
            if not self._woken(carried, cur, dt, fv, fw, s):
                # held on the root exactly: no step, no flicker
                for i, b in enumerate(self.bodies):
                    b.x, b.q = cur[i][0].copy(), cur[i][1].copy()
                r = self.bodies[self.root]
                for i, b in enumerate(self.bodies):
                    if not b.kin:
                        x, q = self.rel[i]
                        b.x = r.x + PB.qrot(r.q, x)
                        b.q = PB.qnorm(PB.qmul(r.q, q))
                self.prev, self.acc = cur, 0.0
                return self._out(s)
            self._wake()
        x1 = cur[self.root][0]
        for b in self.bodies:
            if b.kin:
                continue
            b.x, b.q = carry(b.x, b.q)
            b.v, b.w = PB.qrot(dq, b.v), PB.qrot(dq, b.w)
            b.v = b.v - fv - np.cross(fw, b.com() - x1)
            b.w = b.w - fw
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
                    (x0, q0), (x1, q1) = carried[i], cur[i]
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
        self._watch(dt)
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
