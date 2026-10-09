"""
m2edit.py - small, exact edits on a ported (3.3.5, flat MD20) item
model and its 00.skin. Every function returns new bytes and leaves the input
alone; check_264 is run by the caller on the result.

    subset(model, skin, keep_tris)        only these skin triangles; vertices
                                          compacted in place (unused tail zeroed)
    single_bone(model, skin)              every vertex, particle, ribbon,
                                          attachment, event and light on bone 0;
                                          one-entry bone palette - a piece the
                                          client animates with ONE matrix (the
                                          per-limb piece format)
    translate(model, d)                   every vertex, particle/ribbon/attachment/
                                          light position and bound moved by d
    triangles(model, skin)                [(submesh, (a, b, c) model vertex ids)]
    islands(model, skin)                  connected triangle groups (welded by
                                          position) - the unit a per-limb format cuts by
    repack(model)                         the same model written afresh: only the
                                          data its arrays reference (edits above
                                          leave the source's skeleton, keys and
                                          vertex tail behind as dead bytes)
"""
from __future__ import annotations
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))

import struct

import numpy as np

import retail_m2 as RM

H_BONES, H_KEYLOOK, H_VERTS = 0x2C, 0x34, 0x3C
H_BONELOOK = 0x78
H_ATTACH, H_EVENTS, H_LIGHTS = 0xF0, 0x100, 0x108
H_RIBBONS, H_PARTICLES = 0x120, 0x128
BOUNDS = (0xA0, 0xBC)      # bounding box + radius, collision box + radius
SZ_BONE, SZ_VERT = 88, 48
SZ_ATT, SZ_EVENT, SZ_LIGHT = 40, 36, 156
SZ_RIBBON, SZ_PARTICLE = 176, 476


def _arr(b, at):
    return struct.unpack_from("<II", b, at)


def triangles(model: bytes, skin: bytes):
    sk = RM.skin(skin)
    out = []
    for si, s in enumerate(sk["subs"]):
        idx = sk["tris"][s["istart"]:s["istart"] + s["icount"]]
        for i in range(0, len(idx) - 2, 3):
            out.append((si, (sk["lookup"][idx[i]], sk["lookup"][idx[i + 1]], sk["lookup"][idx[i + 2]])))
    return out


def positions(model: bytes) -> np.ndarray:
    n, o = _arr(model, H_VERTS)
    return np.frombuffer(model, dtype=np.float32, count=n * 12, offset=o).reshape(n, 12)[:, :3].astype(np.float64)


def islands(model: bytes, skin: bytes):
    """[list of triangle indices] per connected component, vertices welded by
    position (a seam split for UVs is one surface)."""
    tris = triangles(model, skin)
    P = np.round(positions(model), 4)
    weld = {}
    vid = np.zeros(len(P), dtype=np.int64)
    for i, p in enumerate(map(tuple, P)):
        vid[i] = weld.setdefault(p, len(weld))
    parent = list(range(len(weld)))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    for _s, (a, b, c) in tris:
        ra, rb, rc = find(vid[a]), find(vid[b]), find(vid[c])
        parent[rb] = ra
        parent[find(rc)] = find(ra)
    groups = {}
    for t, (_s, (a, _b, _c)) in enumerate(tris):
        groups.setdefault(find(vid[a]), []).append(t)
    return list(groups.values()), tris


def palette_mismatches(model: bytes, skin: bytes) -> int:
    """Weighted influences the client skins with a bone other than the
    vertex's own: lookup[boneComboIndex + skin prop] != vertex bone. 0 on a
    sound model; anything else draws a part on the wrong bone (2026-10-09:
    a hood on its flame's billboard turned with the camera)."""
    nl, ol = _arr(model, H_BONELOOK)
    look = np.frombuffer(model, dtype=np.int16, count=nl, offset=ol) if nl else np.zeros(0, np.int16)
    nv, ov = _arr(model, H_VERTS)
    V = np.frombuffer(model, dtype=np.uint8, count=nv * SZ_VERT, offset=ov).reshape(nv, SZ_VERT)
    sk = RM.skin(skin)
    vl = np.asarray(sk["lookup"], dtype=np.int64)
    pr = np.frombuffer(skin, dtype=np.uint8, count=4 * sk["nprops"], offset=sk["oprops"]).reshape(-1, 4)
    bad = 0
    for s in sk["subs"]:
        j = np.arange(s["vstart"], s["vstart"] + s["vcount"])
        if not len(j):
            continue
        mv = vl[j]
        w, own = V[mv, 12:16], V[mv, 16:20].astype(np.int64)
        at = s["bstart"] + pr[j].astype(np.int64)
        eff = np.where(at < len(look), look[np.minimum(at, max(len(look) - 1, 0))] if len(look) else -1, -1)
        bad += int(((w > 0) & (eff != own)).sum())
    return bad


BILLBOARD = 0x78           # bone flags: spherical 0x8, cylindrical 0x10 / 0x20 / 0x40


def single_bone_billboard(model: bytes) -> bool:
    """A one-bone model whose bone is a billboard: wow.exe turns a billboard
    about its pivot to face the camera (0x0082FFED), so the WHOLE piece would
    turn with the camera (a realm-format piece made from a source whose bone 0
    is a glow card)."""
    nb, ob = _arr(model, H_BONES)
    return nb == 1 and bool(struct.unpack_from("<I", model, ob + 4)[0] & BILLBOARD)


JOINT = 0x80000000         # runtime-bound garments: "bind me to the body" (garment_bind)


def mark_body_joints(model: bytes, body_crcs) -> bytes:
    """Bit 31 on every bone that is one of the body's joints (name hash in
    `body_crcs`) AND is weighted or the ancestor of a weighted bone; cleared
    on every other bone. wow.exe never tests bit 31 of a bone's flags (the M2
    animation code 0x00820000..0x00840000 tests only the low byte), so the
    mark changes nothing for the client; a client mod binds marked bones to the
    skeleton that wears the garment (spec 2026-10-09 runtime-bound garments)."""
    m = bytearray(model)
    nb, ob = _arr(m, H_BONES)
    nv, ov = _arr(m, H_VERTS)
    V = np.frombuffer(bytes(m), dtype=np.uint8, count=nv * SZ_VERT, offset=ov).reshape(nv, SZ_VERT)
    weighted = set(np.unique(V[:, 16:20][V[:, 12:16] > 0]).tolist())
    keep = set()
    for b in weighted:
        seen = 0
        while 0 <= b < nb and b not in keep:
            keep.add(b)
            b = struct.unpack_from("<h", m, ob + b * SZ_BONE + 8)[0]
            seen += 1
            if seen > nb:
                raise ValueError("bone parent chain loops")
        if b >= nb:
            raise ValueError("bone parent %d out of range" % b)
    body = set(body_crcs)
    for i in range(nb):
        at = ob + i * SZ_BONE
        fl = struct.unpack_from("<I", m, at + 4)[0] & ~JOINT
        if i in keep and struct.unpack_from("<I", m, at + 12)[0] in body:
            fl |= JOINT
        struct.pack_into("<I", m, at + 4, fl)
    return bytes(m)


def drop_batches(model: bytes, skin: bytes, slots) -> tuple[bytes, int]:
    """(skin, dropped): the skin without the batches that sample any texture
    slot in `slots` (indices into the model's texture array). The submesh
    stays - a submesh no batch names is not drawn."""
    slots = set(slots)
    if not slots:
        return skin, 0
    nl, ol = _arr(model, 0x80)
    look = struct.unpack_from("<%dh" % nl, model, ol) if nl else ()
    s = bytearray(skin)
    nb, ob = struct.unpack_from("<II", s, 4 + 8 * 4)
    kept = []
    for k in range(nb):
        rec = bytes(s[ob + k * 24: ob + k * 24 + 24])
        tc, tco = struct.unpack_from("<HH", rec, 0x0E)
        units = {look[tco + u] for u in range(tc) if tco + u < len(look)}
        if not units & slots:
            kept.append(rec)
    if len(kept) == nb:
        return skin, 0
    s[ob:ob + nb * 24] = b"".join(kept) + bytes((nb - len(kept)) * 24)
    struct.pack_into("<I", s, 4 + 8 * 4, len(kept))
    return bytes(s), nb - len(kept)


def subset(model: bytes, skin: bytes, keep: set[int]):
    """(model, skin) with only triangles `keep` (indices into triangles()),
    submeshes and batches that keep nothing dropped, vertices compacted."""
    m = bytearray(model)
    sk = RM.skin(skin)
    tris = triangles(model, skin)
    nv, ov = _arr(m, H_VERTS)
    used = sorted({v for t in keep for v in tris[t][1]})
    if not used:
        raise ValueError("empty subset")
    remap = {v: i for i, v in enumerate(used)}
    old = bytes(m[ov:ov + nv * SZ_VERT])
    for i, v in enumerate(used):
        m[ov + i * SZ_VERT:ov + (i + 1) * SZ_VERT] = old[v * SZ_VERT:(v + 1) * SZ_VERT]
    m[ov + len(used) * SZ_VERT:ov + nv * SZ_VERT] = bytes((nv - len(used)) * SZ_VERT)
    struct.pack_into("<I", m, H_VERTS, len(used))
    # skin: rebuilt from the kept triangles, submesh by submesh
    by_sub = {}
    for t in sorted(keep):
        si, abc = tris[t]
        by_sub.setdefault(si, []).append(abc)
    look, idx, props, subs, sub_map = [], [], [], [], {}
    for si in sorted(by_sub):
        s = sk["subs"][si]
        local = {}
        vstart, istart = len(look), len(idx)
        for abc in by_sub[si]:
            for v in abc:
                if v not in local:
                    local[v] = len(look) - vstart
                    look.append(remap[v])
                    props.append(0)
                idx.append(local[v] + vstart)
        sub_map[si] = len(subs)
        subs.append((s, vstart, len(look) - vstart, istart, len(idx) - istart))
    if len(look) > 65535:
        raise ValueError("subset has more than 65535 skin vertices")
    raw_subs = []
    so = struct.unpack_from("<II", skin, 4 + 8 * 3)[1]
    for s, vs, vc, ist, ic in subs:
        rec = bytearray(skin[so + sk["subs"].index(s) * 48: so + sk["subs"].index(s) * 48 + 48])
        struct.pack_into("<HHHHHH", rec, 0, s["id"], (ist >> 16) & 0xFFFF, vs, vc, ist & 0xFFFF, ic)
        raw_subs.append(bytes(rec))
    nb, ob = struct.unpack_from("<II", skin, 4 + 8 * 4)
    bats = []
    for k in range(nb):
        rec = bytearray(skin[ob + k * 24: ob + k * 24 + 24])
        si = struct.unpack_from("<H", rec, 4)[0]
        if si in sub_map:
            struct.pack_into("<H", rec, 4, sub_map[si])
            bats.append(bytes(rec))
    if not bats:
        raise ValueError("subset keeps no batch")
    # header 48 bytes (magic + 5 arrays + bone count max), then the arrays
    out = bytearray(48)
    def put(data, align=4):
        while len(out) % align:
            out.append(0)
        o = len(out)
        out.extend(data)
        return o
    o_look = put(struct.pack("<%dH" % len(look), *look))
    o_idx = put(struct.pack("<%dH" % len(idx), *idx))
    o_props = put(bytes(4 * len(look)))
    o_subs = put(b"".join(raw_subs))
    o_bats = put(b"".join(bats))
    struct.pack_into("<4s11I", out, 0, b"SKIN", len(look), o_look, len(idx), o_idx,
                     len(look), o_props, len(raw_subs), o_subs, len(bats), o_bats,
                     sk["max_bones"])
    # props are rebuilt per vertex from the model's own bone indices through
    # the submesh palette: keep the old palette semantics for a skinned piece
    props_b = bytearray(4 * len(look))
    bl_n, bl_o = _arr(m, H_BONELOOK)
    for (s, vs, vc, _ist, _ic) in subs:
        for j in range(vs, vs + vc):
            v = look[j]
            b4 = struct.unpack_from("<4B", m, ov + v * SZ_VERT + 16)
            w4 = struct.unpack_from("<4B", m, ov + v * SZ_VERT + 12)
            pal = list(struct.unpack_from("<%dh" % s["bcount"], m, bl_o + 2 * s["bstart"])) if s["bcount"] else []
            p4 = []
            for b, w in zip(b4, w4):
                p4.append(pal.index(b) if (w and b in pal) else 0)
            struct.pack_into("<4B", props_b, 4 * j, *p4)
    out[o_props:o_props + len(props_b)] = props_b
    return bytes(m), bytes(out)


def compact(model: bytes, skin: bytes):
    """(model, skin, used): only the vertices LOD0's skin draws (retail vertex
    arrays carry every LOD); `used[i]` is the original index of new vertex i.
    Nothing drawn changes."""
    tris = triangles(model, skin)
    used = sorted({v for _s, abc in tris for v in abc})
    m2, s2 = subset(model, skin, set(range(len(tris))))
    return m2, s2, used


def single_bone(model: bytes, skin: bytes):
    """Everything on bone 0, one bone, one-entry palette (per-limb piece
    format: the attachment point's joint moves the whole piece)."""
    m = bytearray(model)
    nb, ob = _arr(m, H_BONES)
    if nb == 0:
        raise ValueError("model has no bone")
    struct.pack_into("<h", m, ob + 8, -1)                  # bone 0 parent: none
    struct.pack_into("<I", m, H_BONES, 1)
    # bone 0 now carries the WHOLE piece: a plain, unanimated joint. The
    # source's bone 0 may be a glow's billboard (Blind Oath's Seraphguards) or
    # a swinging part; either would turn or swing everything.
    struct.pack_into("<iI", m, ob, -1, 0)
    for k in range(3):
        struct.pack_into("<hhIIII", m, ob + 16 + 20 * k, 0, -1, 0, 0, 0, 0)
    nk, ok = _arr(m, H_KEYLOOK)
    for i in range(nk):
        v = struct.unpack_from("<h", m, ok + 2 * i)[0]
        struct.pack_into("<h", m, ok + 2 * i, 0 if v == 0 else -1)
    nl, ol = _arr(m, H_BONELOOK)
    if nl == 0:
        raise ValueError("model has no bone palette")
    struct.pack_into("<h", m, ol, 0)
    struct.pack_into("<I", m, H_BONELOOK, 1)
    nv, ov = _arr(m, H_VERTS)
    for i in range(nv):
        struct.pack_into("<4B4B", m, ov + i * SZ_VERT + 12, 255, 0, 0, 0, 0, 0, 0, 0)
    for at, size, off, fmt in ((H_ATTACH, SZ_ATT, 4, "<H"), (H_EVENTS, SZ_EVENT, 8, "<I"),
                               (H_LIGHTS, SZ_LIGHT, 2, "<h"), (H_RIBBONS, SZ_RIBBON, 4, "<I"),
                               (H_PARTICLES, SZ_PARTICLE, 0x14, "<H")):
        n, o = _arr(m, at)
        for i in range(n):
            struct.pack_into(fmt, m, o + i * size + off, 0)
    s = bytearray(skin)
    sk = RM.skin(skin)
    s[sk["oprops"]:sk["oprops"] + 4 * sk["nprops"]] = bytes(4 * sk["nprops"])
    ns, osub = struct.unpack_from("<II", s, 4 + 8 * 3)
    for k in range(ns):
        struct.pack_into("<HHHH", s, osub + k * 48 + 0xC, 1, 0, 1, 0)
    struct.pack_into("<I", s, 44, 1)
    return bytes(m), bytes(s)


def translate(model: bytes, d) -> bytes:
    """Every vertex and every positioned record moved by d (x, y, z), bounds
    recomputed from the vertices."""
    m = bytearray(model)
    dx, dy, dz = (float(x) for x in d)
    nv, ov = _arr(m, H_VERTS)
    for i in range(nv):
        x, y, z = struct.unpack_from("<3f", m, ov + i * SZ_VERT)
        struct.pack_into("<3f", m, ov + i * SZ_VERT, x + dx, y + dy, z + dz)
    for at, size, off in ((H_ATTACH, SZ_ATT, 8), (H_LIGHTS, SZ_LIGHT, 4),
                          (H_RIBBONS, SZ_RIBBON, 8), (H_PARTICLES, SZ_PARTICLE, 8)):
        n, o = _arr(m, at)
        for i in range(n):
            x, y, z = struct.unpack_from("<3f", m, o + i * size + off)
            struct.pack_into("<3f", m, o + i * size + off, x + dx, y + dy, z + dz)
    nb, ob = _arr(m, H_BONES)
    for i in range(nb):
        x, y, z = struct.unpack_from("<3f", m, ob + i * SZ_BONE + 76)
        struct.pack_into("<3f", m, ob + i * SZ_BONE + 76, x + dx, y + dy, z + dz)
    return bytes(m)


def union_bounds(model: bytes, margin: float = 0.02) -> tuple[bytes, bool]:
    """Header and sequence boxes grown (never shrunk) to contain the bind-pose
    mesh. Retail ships some item boxes that miss their own mesh; the 3.3.5
    client culls an attached model by its box, so the item popped out of view
    on zoom. (model, changed)."""
    m = bytearray(model)
    P = positions(bytes(m))
    if not len(P):
        return bytes(m), False
    mlo, mhi = P.min(0) - margin, P.max(0) + margin
    changed = False

    def grow(at):
        nonlocal changed
        lo = np.array(struct.unpack_from("<3f", m, at))
        hi = np.array(struct.unpack_from("<3f", m, at + 12))
        nlo, nhi = np.minimum(lo, mlo), np.maximum(hi, mhi)
        if (nlo < lo).any() or (nhi > hi).any():
            rad = float(np.linalg.norm(np.maximum(np.abs(nlo), np.abs(nhi))))
            struct.pack_into("<7f", m, at, *nlo, *nhi, max(rad, struct.unpack_from("<f", m, at + 24)[0]))
            changed = True
    for base in BOUNDS:
        grow(base)
    ns, so = _arr(m, 0x1C)
    for i in range(ns):
        grow(so + i * 64 + 0x20)
    return bytes(m), changed


def fit_bounds(model: bytes, margin: float = 0.05) -> bytes:
    """Header bounds and every sequence's bounds = the mesh box (+ margin):
    the client culls an attached model by its current sequence's box, so a
    box a body-height off the mesh makes the piece vanish (Mrgl Floater)."""
    m = bytearray(model)
    P = positions(bytes(m))
    if not len(P):
        return bytes(m)
    lo = P.min(0) - margin
    hi = P.max(0) + margin
    rad = float(np.linalg.norm(np.maximum(np.abs(lo), np.abs(hi))))
    for base in BOUNDS:
        struct.pack_into("<7f", m, base, *lo, *hi, rad)
    ns, so = _arr(m, 0x1C)
    for i in range(ns):
        struct.pack_into("<7f", m, so + i * 64 + 0x20, *lo, *hi, rad)   # M2Sequence bounds + radius
    return bytes(m)


def same_model(a: bytes, b: bytes) -> bool:
    """Every header array, scalar and bound of two models equal (offsets aside)."""
    import m2model as M
    ma, mb = M.read(a, None), M.read(b, None)
    return ((ma.version, ma.flags, ma.nviews, ma.bounds, getattr(ma, "combiner_combos", None))
            == (mb.version, mb.flags, mb.nviews, mb.bounds, getattr(mb, "combiner_combos", None))
            and ma.a == mb.a)


def repack(model: bytes) -> bytes:
    """`model` rewritten by m2model with nothing but referenced data, proven
    equal array by array; the input itself when m2model cannot carry it
    (particles, ribbons, external sequences) or nothing would be saved."""
    import m2model as M
    try:
        if M.external_set(model):
            return model
        out, anims = M.write(M.read(model, None))
    except ValueError:               # particles / ribbons: not modelled by m2model
        return model
    if anims or len(out) >= len(model) or not same_model(model, out):
        return model
    return out
