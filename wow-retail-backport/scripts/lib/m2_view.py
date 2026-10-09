"""
m2_view.py - an M2 skinned, animated and rasterised, to look at and to diff.

    (library: sheet.py draws through mesh() and render())

Why it exists: a converted body can pass every structural gate and still be
wrong in a way only a picture shows - a bone that turns the wrong way, a UV
that lands on the wrong region, an animation that plays another one's keys.
So the converted model is drawn through the same skinning and the same
rasteriser as retail's original, pose for pose, and the two pictures are
compared pixel by pixel. Identical pictures mean the conversion is lossless in
geometry and animation; any difference is printed with where it is.

The animation evaluation follows the format, not a guess:
    bone = parent * T(pivot) * T(translation) * R(rotation) * S(scale) * T(-pivot)
    compressed quaternion component: (s < 0 ? s + 32768 : s - 32767) / 32767
    interpolation 0 holds the key, 1 and 2/3 interpolate (hermite/bezier keys
    are read as their value, which is how the client's own linear fallback
    treats them for character bones)
    a track on a global sequence runs on wall time modulo that loop
"""

from __future__ import annotations
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
import _toolpath  # noqa: E402,F401  (puts scripts/lib on sys.path)

import argparse
import math
import os
import struct
import sys

import numpy as np
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, HERE)

import m2model as M  # noqa: E402



# ---------------------------------------------------------------------------
# animation
# ---------------------------------------------------------------------------
def q16(v):
    return (v + 32768 if v < 0 else v - 32767) / 32767.0


def sample(tr: M.Track, seq: int, t: float, gloops, kind):
    if tr.gseq >= 0:
        s = 0
        loop = gloops[tr.gseq] if tr.gseq < len(gloops) else 0
        t = (t % loop) if loop else 0
    else:
        s = seq
    if s >= len(tr.times) or not tr.times[s]:
        return None
    ts, vs = tr.times[s], tr.values[s]
    if len(ts) != len(vs) or not vs:
        return None

    def conv(v):
        if kind == "quat16":
            return np.array([q16(x) for x in v])
        if isinstance(v, (tuple, list)):
            return np.array(v[:3] if kind == "vec3" else v, dtype=float)
        return np.array([v], dtype=float)
    if len(ts) == 1 or t <= ts[0]:
        return conv(vs[0])
    if t >= ts[-1]:
        return conv(vs[-1])
    i = int(np.searchsorted(ts, t, side="right")) - 1
    a, b = conv(vs[i]), conv(vs[i + 1])
    if tr.interp == 0:
        return a
    f = (t - ts[i]) / max(1e-9, ts[i + 1] - ts[i])
    if kind == "quat16":
        if np.dot(a, b) < 0:
            b = -b
        q = a * (1 - f) + b * f
        return q / max(1e-9, np.linalg.norm(q))
    return a * (1 - f) + b * f


def qmat(q):
    x, y, z, w = q
    return np.array([[1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
                     [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
                     [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)]])


def bone_matrices(m: M.Model, seq: int, t: float):
    bones = m.a["bones"]
    gl = m.a.get("gloops", [])
    mats = [None] * len(bones)

    def get(i):
        if mats[i] is not None:
            return mats[i]
        b = bones[i]
        piv = np.array(b["pivot"])
        local = np.eye(4)
        tr = sample(b["trans"], seq, t, gl, "vec3")
        rot = sample(b["rot"], seq, t, gl, "quat16")
        sc = sample(b["scale"], seq, t, gl, "vec3")
        T = np.eye(4); T[:3, 3] = piv + (tr if tr is not None else 0)
        R = np.eye(4)
        if rot is not None:
            R[:3, :3] = qmat(rot)
        S = np.eye(4)
        if sc is not None:
            S[0, 0], S[1, 1], S[2, 2] = sc
        P = np.eye(4); P[:3, 3] = -piv
        local = T @ R @ S @ P
        if b["parent"] >= 0:
            local = get(b["parent"]) @ local
        mats[i] = local
        return local
    for i in range(len(bones)):
        get(i)
    return np.array(mats)


# ---------------------------------------------------------------------------
# geometry
# ---------------------------------------------------------------------------
def mesh(m: M.Model, skin: dict, geosets, mats):
    """Skinned positions and the triangles of the chosen geosets."""
    raw = m.a["verts"]
    look = skin["lookup"]
    pos = np.array([struct.unpack_from("<3f", raw[li], 0) for li in look])
    w = np.array([struct.unpack_from("<4B", raw[li], 12) for li in look], dtype=float) / 255.0
    # An M2 vertex names its bones directly (absolute bone indices); only the
    # skin's per-submesh palette goes through the bone lookup. a body converter's
    # palettes() asserts look[palette + prop] == vertex byte on every weighted
    # influence, which is this same fact.
    bi = np.array([struct.unpack_from("<4B", raw[li], 16) for li in look])
    if len(mats) and (bi[w > 0] >= len(mats)).any():
        raise ValueError("vertex names a bone the model does not have")
    uv = np.array([struct.unpack_from("<2f", raw[li], 32) for li in look])
    hp = np.concatenate([pos, np.ones((len(pos), 1))], axis=1)
    out = np.zeros_like(pos)
    for k in range(4):
        M4 = mats[bi[:, k]]                                  # (n,4,4)
        p = np.einsum("nij,nj->ni", M4, hp)[:, :3]
        out += p * w[:, k:k + 1]
    tris = []
    subs = []
    for si, s in enumerate(skin["subs"]):
        if s["id"] not in geosets:
            continue
        a, n = s["istart"], s["icount"]
        t = skin["tris"][a:a + n]
        tris.extend(t)
        subs.extend([si] * (n // 3))
    return out, np.array(tris, dtype=np.int64).reshape(-1, 3), uv, np.array(subs)


def render(pos, tris, size=512, yaw=0.0, frame=None, tex=None, uv=None,
           face_ids=None, texture_faces=None, texture_by_face=None,
           texture_alpha=False, surface=None, flat_color=None,
           return_depth=False, fragment_shader=None, blend_mode=0,
           depth_write=True, depth_test=True):
    """Orthographic, z-buffered, Lambert-shaded. M2 space is X forward, Y left,
    Z up; the camera looks at the character's front (from +X) turned by yaw."""
    c, s = math.cos(yaw), math.sin(yaw)
    # screen x = -y' (right), screen y = -z (down), depth = x'
    x = pos[:, 0] * c - pos[:, 1] * s
    y = pos[:, 0] * s + pos[:, 1] * c
    z = pos[:, 2]
    if frame is None:
        frame = (-y.max(), -y.min(), z.min(), z.max())
    sx0, sx1, z0, z1 = frame
    span = max(sx1 - sx0, z1 - z0) * 1.08
    cx, cz = (sx0 + sx1) / 2, (z0 + z1) / 2
    px = ((-y - cx) / span + 0.5) * size
    py = ((cz - z) / span + 0.5) * size
    depth = x
    if surface is None:
        img = np.full((size, size, 3), 40, np.float32)
        zb = np.full((size, size), -1e9, np.float32)
    else:
        img = np.asarray(surface[0], dtype=np.float32).copy()
        zb = np.asarray(surface[1], dtype=np.float32).copy()
        if img.shape != (size, size, 3) or zb.shape != (size, size):
            raise ValueError("render surface image/depth does not match output size")
    v0, v1, v2 = tris[:, 0], tris[:, 1], tris[:, 2]
    P = np.stack([pos[:, 0] * c - pos[:, 1] * s, pos[:, 0] * s + pos[:, 1] * c, pos[:, 2]], 1)
    nrm = np.cross(P[v1] - P[v0], P[v2] - P[v0])
    nrm /= np.maximum(1e-9, np.linalg.norm(nrm, axis=1, keepdims=True))
    light = np.array([0.6, -0.35, 0.72]); light /= np.linalg.norm(light)
    shade = np.clip(np.abs(nrm @ light), 0.15, 1.0)
    for ti in range(len(tris)):
        a, b, cc = v0[ti], v1[ti], v2[ti]
        xs = (px[a], px[b], px[cc]); ys = (py[a], py[b], py[cc])
        minx, maxx = int(max(0, math.floor(min(xs)))), int(min(size - 1, math.ceil(max(xs))))
        miny, maxy = int(max(0, math.floor(min(ys)))), int(min(size - 1, math.ceil(max(ys))))
        if minx > maxx or miny > maxy:
            continue
        gx, gy = np.meshgrid(np.arange(minx, maxx + 1) + 0.5, np.arange(miny, maxy + 1) + 0.5)
        d = (ys[1] - ys[2]) * (xs[0] - xs[2]) + (xs[2] - xs[1]) * (ys[0] - ys[2])
        if abs(d) < 1e-12:
            continue
        l0 = ((ys[1] - ys[2]) * (gx - xs[2]) + (xs[2] - xs[1]) * (gy - ys[2])) / d
        l1 = ((ys[2] - ys[0]) * (gx - xs[2]) + (xs[0] - xs[2]) * (gy - ys[2])) / d
        l2 = 1 - l0 - l1
        inside = (l0 >= 0) & (l1 >= 0) & (l2 >= 0)
        if not inside.any():
            continue
        dz = l0 * depth[a] + l1 * depth[b] + l2 * depth[cc]
        sub = zb[miny:maxy + 1, minx:maxx + 1]
        win = inside & (dz >= sub - 1e-5 if fragment_shader is not None else dz > sub)
        if not depth_test:
            win = inside
        if not win.any():
            continue
        if fragment_shader is not None:
            rgba = fragment_shader(ti, (l0, l1, l2), shade[ti])
            color = np.asarray(rgba[..., :3], dtype=np.float32) * 255.0
            alpha = np.clip(rgba[..., 3:4], 0, 1)
            if blend_mode == 1:
                win &= alpha[..., 0] >= .5
            reg = img[miny:maxy + 1, minx:maxx + 1]
            if blend_mode in (0, 1):
                result = color
            elif blend_mode == 2:
                result = color * alpha + reg * (1 - alpha)
            elif blend_mode == 3:
                result = reg + color
            elif blend_mode == 4:
                result = reg + color * alpha
            elif blend_mode == 5:
                result = reg * color / 255.0
            elif blend_mode == 6:
                result = reg * color * 2 / 255.0
            elif blend_mode == 7:
                result = color + reg * (1 - alpha)
            else:
                raise ValueError('unsupported material blend mode %d' % blend_mode)
            reg[win] = np.clip(result, 0, 255)[win]
            if depth_write:
                sub[win] = dz[win]
            continue
        face_texture = (texture_by_face.get(int(face_ids[ti]))
                        if texture_by_face is not None and face_ids is not None else tex)
        if flat_color is not None:
            col = np.asarray(flat_color, dtype=np.float32) * shade[ti]
        elif face_texture is not None and uv is not None \
                and (texture_faces is None or texture_faces[ti]):
            u = l0 * uv[a, 0] + l1 * uv[b, 0] + l2 * uv[cc, 0]
            v = l0 * uv[a, 1] + l1 * uv[b, 1] + l2 * uv[cc, 1]
            th, tw = face_texture.shape[:2]
            tx = np.clip((u % 1.0) * tw, 0, tw - 1).astype(int)
            ty = np.clip((v % 1.0) * th, 0, th - 1).astype(int)
            sample = face_texture[ty, tx]
            col = sample[..., :3] * (0.45 + 0.55 * shade[ti])
            alpha = (sample[..., 3:4] / 255.0 if texture_alpha and sample.shape[-1] > 3
                     else np.ones((*win.shape, 1), dtype=np.float32))
            if texture_alpha and sample.shape[-1] > 3:
                write_depth = win & (alpha[..., 0] >= 0.98)
                sub[write_depth] = dz[write_depth]
                reg = img[miny:maxy + 1, minx:maxx + 1]
                reg[win] = (col * alpha + reg * (1.0 - alpha))[win]
                continue
        else:
            col = np.array([200, 190, 175], np.float32) * shade[ti]
        sub[win] = dz[win]
        reg = img[miny:maxy + 1, minx:maxx + 1]
        reg[win] = col[win] if col.ndim == 3 else col
    result = img.astype(np.uint8)
    if return_depth:
        return result, frame, zb
    return result, frame


# ---------------------------------------------------------------------------
def default_geosets(skin):
    """One mesh per group: the group's lowest variant, except where retail's
    own defaults differ (hair 1 = bald scalp cap? keep the first real style)."""
    ids = sorted({s["id"] for s in skin["subs"]})
    by = {}
    for i in ids:
        by.setdefault(i // 100, []).append(i)
    out = {0}
    for g, L in by.items():
        if g == 0:
            out.add(next((i for i in L if i > 0), 0))
            continue
        pick = {7: 702, 32: 3202, 22: 2201, 17: 1701, 13: 1301, 33: 3301}.get(g)
        out.add(pick if pick in L else L[0])
    return out


def seq_index(m, anim_id, var=0):
    for i, s in enumerate(m.a["seqs"]):
        if s["data"][0] == anim_id and s["data"][1] == var:
            return i
    return None


if __name__ == "__main__":
    raise SystemExit("library module: run `python bp.py sheet` instead")
