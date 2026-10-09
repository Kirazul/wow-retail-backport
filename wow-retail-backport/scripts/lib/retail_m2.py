"""
retail_m2.py - read the chunked containers retail ships character art in.

Retail splits what a 3.3.5 client reads as ONE file across several:

    .m2     MD21 (the classic header + arrays) and side chunks naming the
            other files by FileDataID: SFID skins, TXID textures, AFID the
            external animations, SKID the skeleton, BFID bone files, ...
    .skel   SKL1 header, SKA1 attachments, SKB1 bones + key-bone lookup,
            SKS1 global loops + sequences + sequence lookup, SKPD the parent
            skeleton, and its own AFID / BFID
    .anim   AFM2 (keyframe data) and, when the skeleton is shared, AFSA / AFSB

Nothing here interprets geometry; it only splits a file into its chunks and
decodes the ones that name other files. The converters consume
the payloads.
"""

from __future__ import annotations
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
import _toolpath  # noqa: E402,F401  (puts scripts/lib on sys.path)

import struct


def chunks(data: bytes) -> dict[str, list[bytes]]:
    """Every top-level chunk, by magic. A legacy flat MD20 file yields
    {"MD20": [data]} so callers can treat both the same way."""
    if data[:4] == b"MD20":
        return {"MD20": [data]}
    out: dict[str, list[bytes]] = {}
    o = 0
    n = len(data)
    while o + 8 <= n:
        magic = data[o:o + 4].decode("latin1")
        size = struct.unpack_from("<I", data, o + 4)[0]
        if o + 8 + size > n:
            raise ValueError("chunk %s at %d overruns the file (%d > %d)"
                             % (magic, o, o + 8 + size, n))
        out.setdefault(magic, []).append(data[o + 8:o + 8 + size])
        o += 8 + size
    if o != n:
        raise ValueError("trailing %d bytes after the last chunk" % (n - o))
    return out


def md20(data: bytes) -> bytes:
    """The classic MD20 block of either container. Offsets inside it are
    relative to its own start in both, so it reads the same way."""
    c = chunks(data)
    if "MD20" in c:
        return c["MD20"][0]
    if "MD21" in c:
        return c["MD21"][0]
    raise ValueError("no MD20/MD21 payload")


def vertices(m: bytes):
    """[(pos3, weights4, bones4, normal3, uv2, uv2b)] of an MD20 block."""
    n, o = struct.unpack_from("<II", m, 0x3C)
    out = []
    for i in range(n):
        b = o + i * 48
        p = struct.unpack_from("<3f", m, b)
        w = struct.unpack_from("<4B", m, b + 12)
        bi = struct.unpack_from("<4B", m, b + 16)
        nr = struct.unpack_from("<3f", m, b + 20)
        uv = struct.unpack_from("<2f", m, b + 32)
        uv2 = struct.unpack_from("<2f", m, b + 40)
        out.append((p, w, bi, nr, uv, uv2))
    return out


def skin(data: bytes) -> dict:
    """A .skin (SKIN magic, 3.3.5 and retail alike): vertex lookup, triangle
    indices (into the lookup), submeshes and batches."""
    if data[:4] != b"SKIN":
        raise ValueError("not a skin")
    (ni, oi, nt, ot, npr, opr, ns, os_, nb, ob, maxb) = struct.unpack_from(
        "<11I", data, 4)
    look = list(struct.unpack_from("<%dH" % ni, data, oi))
    tris = list(struct.unpack_from("<%dH" % nt, data, ot))
    subs = []
    for i in range(ns):
        b = os_ + i * 48
        # bstart = boneComboIndex (+0x0E): the submesh's palette is
        # boneLookup[bstart : bstart + bcount]; +0x10 is boneInfluences
        (sid, level, vstart, vcount, istart, icount, bcount, bstart,
         binf, center_bone) = struct.unpack_from("<HHHHHHHHHH", data, b)
        subs.append({"id": sid, "level": level, "vstart": vstart,
                     "vcount": vcount, "istart": istart | (level << 16),
                     "icount": icount, "bcount": bcount, "bstart": bstart,
                     "binf": binf, "center_bone": center_bone})
    bats = []
    for i in range(nb):
        b = ob + i * 24
        bats.append(struct.unpack_from("<BbHHHhHHHHHHH", data, b))
    return {"lookup": look, "tris": tris, "subs": subs, "batches": bats,
            "max_bones": maxb, "nprops": npr, "oprops": opr}


def bones(m: bytes):
    """[(keyBoneId, flags, parent, submesh, pivot3)] of an MD20 block."""
    n, o = struct.unpack_from("<II", m, 0x2C)
    out = []
    for i in range(n):
        b = o + i * 88
        kb, fl, par, sub = struct.unpack_from("<iIhH", m, b)
        piv = struct.unpack_from("<3f", m, b + 76)
        out.append((kb, fl, par, sub, piv))
    return out


def bone_crcs(m: bytes) -> list[int]:
    """boneNameCRC of every bone (+0x0C of the 88-byte record). Retail keeps
    one name hash per joint across every body, which is what lets a joint be
    found in a skeleton by WHAT it is rather than where it sits."""
    n, o = struct.unpack_from("<II", m, 0x2C)
    return [struct.unpack_from("<I", m, o + i * 88 + 12)[0] for i in range(n)]


def attachments(m: bytes):
    """[(id, bone, pos3)] - M2Attachment is 40 bytes: id, bone, unknown,
    position, then the animate_attached track."""
    n, o = struct.unpack_from("<II", m, 0xF0)
    out = []
    for i in range(n):
        b = o + i * 40
        aid, bone = struct.unpack_from("<IH", m, b)
        pos = struct.unpack_from("<3f", m, b + 8)
        out.append((aid, bone, pos))
    return out


def u32s(b: bytes) -> list[int]:
    return list(struct.unpack_from("<%dI" % (len(b) // 4), b, 0))


def afid(b: bytes) -> list[tuple[int, int, int]]:
    """AFID -> [(animId, subAnimId, fdid)], fdid 0 meaning "in the file"."""
    return [struct.unpack_from("<HHI", b, i * 8) for i in range(len(b) // 8)]


def deps(data: bytes) -> dict[str, list[int]]:
    """FileDataIDs one .m2 or .skel names, grouped by what they are."""
    c = chunks(data)
    d: dict[str, list[int]] = {}
    if "SFID" in c:
        d["skin"] = [x for x in u32s(c["SFID"][0]) if x]
    if "TXID" in c:
        d["texture"] = [x for x in u32s(c["TXID"][0]) if x]
    if "AFID" in c:
        d["anim"] = sorted({f for _a, _s, f in afid(c["AFID"][0]) if f})
    if "SKID" in c:
        d["skel"] = [x for x in u32s(c["SKID"][0]) if x]
    if "BFID" in c:
        d["bone"] = [x for x in u32s(c["BFID"][0]) if x]
    if "PFID" in c:
        d["phys"] = [x for x in u32s(c["PFID"][0]) if x]
    if "SKPD" in c:
        b = c["SKPD"][0]
        parent = struct.unpack_from("<I", b, 8)[0] if len(b) >= 12 else 0
        if parent:
            d["skel"] = d.get("skel", []) + [parent]
    if "RPID" in c:
        d["recursive"] = [x for x in u32s(c["RPID"][0]) if x]
    if "GPID" in c:
        d["geometry_particle"] = [x for x in u32s(c["GPID"][0]) if x]
    return d
