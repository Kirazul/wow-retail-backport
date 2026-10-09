r"""
physread.py - retail runtime physics (Blizzard's Domino engine, .phys),
read exactly as the format is documented (wowdev.wiki PHYS, oldid 30458,
read 2026-10-09) and baked offline into looping keyframes the 3.3.5a client
plays: it has no physics and no hook of ours adds any (never-override rule,
and Warden).

    python bp.py phys --work W <model fdid>          (dump + optional settle)

FORMAT (chunked; PHYS version, then unordered chunks)
  BODY/BDY2/BDY3/BDY4  type (file 1 = dynamic; 0 = the kinematic root that
                       follows its bone; other = static), bone, position,
                       shape range [, BDY3+: 0x18 gravity scale, 0x20 linear
                       and 0x24 angular damping]. 0x18 is undocumented; it is
                       the gravity scale by measurement (304 Midnight models):
                       bodies with it < 0 sit ABOVE their parent in 92% of
                       joints (plumes, flames: they float), > 0 mostly below.
  SHAP/SHP2            shape type (0 box BOXS, 1 capsule CAPS, 2 sphere SPHS,
                       3 polytope PLYT) + index, friction, restitution, density
  JOIN                 bodyA, bodyB, type (0 spherical SPHJ, 1 shoulder SHOJ/SHJ2,
                       2 weld WELJ/WLJ2/WLJ3, 3 revolute REVJ/REV2, 4 prismatic
                       PRSJ/PRS2, 5 distance DSTJ), index into its chunk
  joint frames are mat3x4 (rotation columns + translation) in each body's
  space; a shoulder joint limits twist [lower, upper] about the frame z and a
  cone, all in DEGREES (measured: cones 12/30/45/60, twists +-2..20); SHOJ v2+,
  REVJ/PRSJ carry a motor (max torque/force, uint32 mode 1 = position, and
  for SHJ2/REV2/PRS2 frequency Hz + damping ratio) that springs the joint to
  its frames' alignment; a weld is soft (angular frequency Hz, damping ratio).
"""
from __future__ import annotations
import os as _os, sys as _sys

import struct

import numpy as np

import retail_m2 as RM


def _chunks(data: bytes):
    out = {}
    o = 0
    while o + 8 <= len(data):
        # tags are stored as little-endian uint32s: "SYHP" on disk is PHYS
        tag = data[o:o + 4][::-1].decode("latin-1")
        n = struct.unpack_from("<I", data, o + 4)[0]
        out[tag] = data[o + 8:o + 8 + n]
        o += 8 + n
    return out


def _mat34(b, o):
    """mat3x4 as stored: 12 floats; returned as a 4x4 (rotation 3x3 + translation)."""
    f = struct.unpack_from("<12f", b, o)
    m = np.eye(4)
    m[:3, :3] = np.array(f[:9]).reshape(3, 3).T if False else np.array([[f[0], f[3], f[6]],
                                                                         [f[1], f[4], f[7]],
                                                                         [f[2], f[5], f[8]]])
    m[:3, 3] = f[9:12]
    return m


def physics_blob(files, model_fdid: int) -> bytes | None:
    """The PHYS data of a retail model: inline PFDC, else the PFID file.
    `files`: a wago.Files (downloads on demand)."""
    raw = files.try_read(int(model_fdid))
    if not raw:
        return None
    c = RM.chunks(raw)
    if "PFDC" in c:
        return c["PFDC"][0]
    if "PFID" in c:
        fid = struct.unpack_from("<I", c["PFID"][0], 0)[0]
        return files.try_read(fid)
    return None


def parse(blob: bytes) -> dict:
    """{version, bodies[], shapes[], joints[]} with every documented field."""
    if blob[:4] not in (b"PHYS", b"SYHP"):
        raise ValueError("not a PHYS block")
    ch = _chunks(blob)
    version = struct.unpack_from("<h", ch["PHYS"], 0)[0]
    bodies = []
    for tag, size in (("BDY4", 0x30), ("BDY3", 0x2C), ("BDY2", 0x20), ("BODY", 0x1C)):
        if tag in ch:
            b = ch[tag]
            for i in range(len(b) // size):
                o = i * size
                if tag in ("BDY3", "BDY4"):
                    typ, bone = struct.unpack_from("<HH", b, o)
                    pos = struct.unpack_from("<3f", b, o + 4)
                    shape0, nshape = struct.unpack_from("<Hxxi", b, o + 0x10)
                    gscale = struct.unpack_from("<f", b, o + 0x18)[0]
                    drag = struct.unpack_from("<f", b, o + 0x20)[0] if size >= 0x24 else 0.0
                    weight = struct.unpack_from("<f", b, o + 0x24)[0] if size >= 0x28 else 0.0
                else:
                    gscale = 1.0
                    typ = struct.unpack_from("<H", b, o)[0]
                    pos = struct.unpack_from("<3f", b, o + 4)
                    bone = struct.unpack_from("<H", b, o + 0x10)[0]
                    shape0, nshape = struct.unpack_from("<ii", b, o + 0x14)
                    drag, weight = 0.0, 0.0
                bodies.append({"type": typ, "bone": bone, "pos": np.array(pos), "shape0": shape0,
                               "nshape": nshape, "gravity": gscale, "drag": drag, "angdrag": weight,
                               "weight": weight, "tag": tag})
            break
    shapes = []
    for tag, size in (("SHP2", 0x20), ("SHAP", 0x14)):
        if tag in ch:
            b = ch[tag]
            for i in range(len(b) // size):
                st, si = struct.unpack_from("<hh", b, i * size)
                fr, rs, den = struct.unpack_from("<3f", b, i * size + 8)
                shapes.append({"type": st, "index": si, "friction": fr, "restitution": rs, "density": den})
            break
    boxes = [(_mat34(ch["BOXS"], i * 60), np.array(struct.unpack_from("<3f", ch["BOXS"], i * 60 + 48)))
             for i in range(len(ch.get("BOXS", b"")) // 60)]
    caps = [(np.array(struct.unpack_from("<3f", ch["CAPS"], i * 28)),
             np.array(struct.unpack_from("<3f", ch["CAPS"], i * 28 + 12)),
             struct.unpack_from("<f", ch["CAPS"], i * 28 + 24)[0])
            for i in range(len(ch.get("CAPS", b"")) // 28)]
    sphs = [(np.array(struct.unpack_from("<3f", ch["SPHS"], i * 16)), struct.unpack_from("<f", ch["SPHS"], i * 16 + 12)[0])
            for i in range(len(ch.get("SPHS", b"")) // 16)]
    plys = []
    if "PLYT" in ch:
        b = ch["PLYT"]
        n = struct.unpack_from("<I", b, 0)[0]
        heads = []
        for i in range(n):
            o = 4 + i * 0x50
            vc = struct.unpack_from("<I", b, o)[0]
            c10 = struct.unpack_from("<I", b, o + 0x10)[0]
            nc = struct.unpack_from("<I", b, o + 0x28)[0]
            heads.append((vc, c10, nc))
        o = 4 + n * 0x50
        for vc, c10, nc in heads:
            v = np.array(struct.unpack_from("<%df" % (vc * 3), b, o)).reshape(vc, 3)
            plys.append(v)
            o += vc * 12 + c10 * 16 + c10 + nc * 4
    for s in shapes:
        if s["type"] == 0 and s["index"] < len(boxes):
            s["box"] = boxes[s["index"]]
        elif s["type"] == 1 and s["index"] < len(caps):
            s["capsule"] = caps[s["index"]]
        elif s["type"] == 2 and s["index"] < len(sphs):
            s["sphere"] = sphs[s["index"]]
        elif s["type"] == 3 and s["index"] < len(plys):
            s["polytope"] = plys[s["index"]]
    joints = []
    J = ch.get("JOIN", b"")
    sub = {}
    for tag, size in (("SPHJ", 28), ("SHJ2", 0x7C), ("SHOJ", 0x74 if version >= 2 else 0x6C),
                      ("WLJ3", 0x74), ("WLJ2", 0x70), ("WELJ", 0x68), ("REV2", 0x78), ("REVJ", 0x70),
                      ("PRS2", 0x80), ("PRSJ", 0x78), ("DSTJ", 0x1C)):
        if tag in ch:
            kind = {"SPHJ": 0, "SHJ2": 1, "SHOJ": 1, "WLJ3": 2, "WLJ2": 2, "WELJ": 2, "REV2": 3, "REVJ": 3,
                    "PRS2": 4, "PRSJ": 4, "DSTJ": 5}[tag]
            if kind not in sub:
                sub[kind] = (tag, ch[tag], size)
    for i in range(len(J) // 16):
        a, b_, _u, jt, jid = struct.unpack_from("<II4shh", J, i * 16)
        j = {"a": a, "b": b_, "type": jt, "id": jid}
        if jt in sub:
            tag, d, size = sub[jt]
            o = jid * size
            if o + size <= len(d):
                if jt == 0:
                    j["anchorA"] = np.array(struct.unpack_from("<3f", d, o))
                    j["anchorB"] = np.array(struct.unpack_from("<3f", d, o + 12))
                    j["friction"] = struct.unpack_from("<f", d, o + 24)[0]
                elif jt == 5:
                    j["anchorA"] = np.array(struct.unpack_from("<3f", d, o))
                    j["anchorB"] = np.array(struct.unpack_from("<3f", d, o + 12))
                    j["dist"] = struct.unpack_from("<f", d, o + 24)[0]
                else:
                    j["frameA"] = _mat34(d, o)
                    j["frameB"] = _mat34(d, o + 0x30)
                    v = struct.unpack_from("<%df" % ((size - 0x60) // 4), d, o + 0x60)
                    u = struct.unpack_from("<%dI" % ((size - 0x60) // 4), d, o + 0x60)

                    def motor(tq, mode, fq=None):
                        j["motor"] = {"max": v[tq], "mode": u[mode],
                                      "freq": v[fq] if fq is not None and fq < len(v) else 0.0,
                                      "damp": v[fq + 1] if fq is not None and fq + 1 < len(v) else 0.0}
                    if jt == 1:
                        j["twist"] = (v[0], v[1])                       # degrees
                        j["cone"] = np.radians(min(max(v[2], 0.0), 179.0))
                        if len(v) >= 5:
                            motor(3, 4, 5)
                    elif jt == 2:
                        j["freq"], j["damp"] = v[0], v[1]               # angular Hz, ratio
                    elif jt == 3:
                        j["limits"] = (v[0], v[1])                      # degrees
                        if len(v) >= 4:
                            motor(2, 3, 4)
                    elif jt == 4:
                        j["limits"] = (v[0], v[1])
                        if len(v) >= 6:
                            motor(3, 5, 6)
        joints.append(j)
    return {"version": version, "bodies": bodies, "shapes": shapes, "joints": joints}


def dump(p: dict) -> list[str]:
    out = ["version %s: %d bodies, %d shapes, %d joints" % (p["version"], len(p["bodies"]), len(p["shapes"]),
                                                          len(p["joints"]))]
    for i, b in enumerate(p["bodies"]):
        out.append("body %d: %s, bone %d, at %s, shapes %d..%d, gravity x%.2f, drag %.2f (%s)" % (
            i, {0: "kinematic root (follows its bone)", 1: "dynamic (simulated)"}.get(b["type"], "static"),
            b["bone"], np.round(b["pos"], 3).tolist(), b["shape0"], b["shape0"] + b["nshape"] - 1,
            b["gravity"], b["drag"], b["tag"]))
    for i, sh in enumerate(p["shapes"]):
        kind = next((k for k in ("box", "capsule", "sphere", "polytope") if k in sh), "?")
        out.append("shape %d: %s, density %.2f, friction %.2f, restitution %.2f" % (
            i, kind, sh["density"], sh["friction"], sh["restitution"]))
    names = {0: "spherical", 1: "shoulder (cone + twist)", 2: "weld (soft)", 3: "revolute (hinge)",
             4: "prismatic", 5: "distance"}
    for j in p["joints"]:
        extra = {k: (np.round(v, 3).tolist() if hasattr(v, "shape") else v)
                 for k, v in j.items() if k not in ("a", "b", "type", "id", "frameA", "frameB")}
        out.append("joint %s: body %d -> body %d %s" % (names.get(j["type"], j["type"]), j["a"], j["b"], extra))
    return out


if __name__ == "__main__":
    raise SystemExit("library module: run `python bp.py phys --work W <fdid>`")
