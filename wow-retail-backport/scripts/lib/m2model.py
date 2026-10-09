"""
m2model.py - an M2 read into Python objects and written back as version 264.

WHY A FULL MODEL AND NOT A BYTE PATCH
-------------------------------------
m2_port.py changes a retail model in place and proves every changed byte, which
is the right tool when nothing moves. A retail CHARACTER body cannot be done
that way: its skeleton lives in a separate .skel (Zandalari), its animations in
.anim files whose chunk layout 3.3.5 does not read, it carries sequences a
3.3.5 client can never ask for, and its skin has to be rebuilt. Arrays change
length, so offsets change, so the file has to be WRITTEN.

What keeps that honest is the round trip: read a model this client already
draws, write it, read the result, and compare every element of every array -
`python tools/lib/m2model.py --roundtrip <client path>...`. A field this module
gets wrong cannot survive that test on the client's own bodies.

LAYOUT (version 264; retail's MD21 payload has the same header and records)
---------------------------------------------------------------------------
Every element type is described once in SCHEMA as a flat list of fields:

    ("u32", name) ("i16", name) ... plain values
    ("arr", name, elem)            M2Array of `elem` (a fixed struct format
                                   string, or the name of another record)
    ("track", name, value_fmt)     M2Track: interpolation, global sequence,
                                   then per-SEQUENCE arrays of timestamps and
                                   values - the only data that may live in an
                                   external .anim file
    ("trackbase", name)            M2TrackBase: timestamps only (events)

A track's per-sequence arrays are kept per sequence index, so dropping or
reordering sequences is a list operation and the writer decides, per sequence,
whether its keyframes go inline or into that sequence's .anim.
"""

from __future__ import annotations
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
import _toolpath  # noqa: E402,F401  (every tools/ folder and re/ on sys.path)

import argparse
import struct
import sys

# ---------------------------------------------------------------------------
# value formats used inside tracks
# ---------------------------------------------------------------------------
VAL = {
    "vec3": "<3f", "quat16": "<4h", "quatf": "<4f", "fixed16": "<h",
    "u8": "<B", "f32": "<f", "u16": "<H", "u32": "<I",
    # spline keys (cameras): value, in-tangent, out-tangent
    "spl_vec3": "<9f", "spl_f32": "<3f",
}

# ---------------------------------------------------------------------------
# record schemas
# ---------------------------------------------------------------------------
SCHEMA: dict[str, list] = {
    "sequence": [("raw", "data", "<HHIfIhHIIIfffffffhH")],    # 64 bytes
    "bone": [("i32", "key"), ("u32", "flags"), ("i16", "parent"),
             ("u16", "submesh"), ("u32", "crc"),
             ("track", "trans", "vec3"), ("track", "rot", "quat16"),
             ("track", "scale", "vec3"), ("raw", "pivot", "<3f")],
    "color": [("track", "rgb", "vec3"), ("track", "alpha", "fixed16")],
    "texture": [("u32", "type"), ("u32", "flags"), ("arr", "name", "<c")],
    "texweight": [("track", "weight", "fixed16")],
    "textransform": [("track", "trans", "vec3"), ("track", "rot", "quatf"),
                     ("track", "scale", "vec3")],
    "material": [("raw", "data", "<HH")],
    "attachment": [("u32", "id"), ("u16", "bone"), ("u16", "unk"),
                   ("raw", "pos", "<3f"), ("track", "animate", "u8")],
    "event": [("u32", "ident"), ("u32", "data"), ("u32", "bone"),
              ("raw", "pos", "<3f"), ("trackbase", "enabled")],
    "light": [("u16", "type"), ("i16", "bone"), ("raw", "pos", "<3f"),
              ("track", "amb_color", "vec3"), ("track", "amb_int", "f32"),
              ("track", "diff_color", "vec3"), ("track", "diff_int", "f32"),
              ("track", "att_start", "f32"), ("track", "att_end", "f32"),
              ("track", "visible", "u8")],
    # Cataclysm moved the field of view from a float into a track, so the
    # record is 100 bytes in 264 and 116 in retail; read_record picks by version.
    "camera": [("u32", "type"), ("f32", "fov"), ("f32", "far"), ("f32", "near"),
               ("track", "pos", "spl_vec3"), ("raw", "pos_base", "<3f"),
               ("track", "target", "spl_vec3"), ("raw", "target_base", "<3f"),
               ("track", "roll", "spl_f32")],
    "camera274": [("u32", "type"), ("f32", "far"), ("f32", "near"),
                  ("track", "pos", "spl_vec3"), ("raw", "pos_base", "<3f"),
                  ("track", "target", "spl_vec3"), ("raw", "target_base", "<3f"),
                  ("track", "roll", "spl_f32"), ("track", "fov", "spl_f32")],
}

# header arrays in file order, with their element kind
HEADER = [
    (0x08, "name", "<c"), (0x14, "gloops", "<I"), (0x1C, "seqs", "sequence"),
    (0x24, "seqlookup", "<h"), (0x2C, "bones", "bone"),
    (0x34, "keybones", "<h"), (0x3C, "verts", "<48s"),
    (0x48, "colors", "color"), (0x50, "textures", "texture"),
    (0x58, "texweights", "texweight"), (0x60, "textransforms", "textransform"),
    (0x68, "replacetex", "<h"), (0x70, "materials", "material"),
    (0x78, "bonelookup", "<h"), (0x80, "texlookup", "<h"),
    (0x88, "texunitlookup", "<h"), (0x90, "translookup", "<h"),
    (0x98, "textranslookup", "<h"),
    (0xD8, "colltris", "<H"), (0xE0, "collverts", "<3f"),
    (0xE8, "collnorms", "<3f"), (0xF0, "attach", "attachment"),
    (0xF8, "attachlookup", "<h"), (0x100, "events", "event"),
    (0x108, "lights", "light"), (0x110, "cameras", "camera"),
    (0x118, "cameralookup", "<h"), (0x120, "ribbons", None),
    (0x128, "particles", None),
]
HEADER_SIZE = 0x130


def rec_size(kind) -> int:
    if kind is None:
        return 0
    if isinstance(kind, str) and kind.startswith("<"):
        return struct.calcsize(kind)
    n = 0
    for f in SCHEMA[kind]:
        t = f[0]
        if t in ("u32", "i32", "f32"):
            n += 4
        elif t in ("u16", "i16"):
            n += 2
        elif t == "raw":
            n += struct.calcsize(f[2])
        elif t == "arr":
            n += 8
        elif t == "track":
            n += 20
        elif t == "trackbase":
            n += 12
    return n


class Track:
    """interp, gseq, and per-sequence [timestamps], [values] lists."""
    __slots__ = ("interp", "gseq", "times", "values", "fmt")

    def __init__(self, interp, gseq, times, values, fmt):
        self.interp, self.gseq = interp, gseq
        self.times, self.values, self.fmt = times, values, fmt

    def __eq__(self, o):
        return (self.interp, self.gseq, self.times, self.values) == \
            (o.interp, o.gseq, o.times, o.values)


# ---------------------------------------------------------------------------
# reading
# ---------------------------------------------------------------------------
class Source:
    """Where a nested array's bytes come from.

    `main` is the buffer the record lives in. For keyframes, sequence s may
    live elsewhere: `ext(s)` returns that sequence's buffer (an .anim payload)
    or None when it is in `main`.
    """

    def __init__(self, main: bytes, ext=None):
        self.main, self.ext = main, ext

    def seq_buf(self, s):
        if self.ext is None:
            return self.main
        b = self.ext(s)
        return self.main if b is None else b


def _arr(buf, n, o, fmt):
    sz = struct.calcsize(fmt)
    if n == 0 or buf == b"":
        # an empty buffer is an external sequence with no file behind it - one
        # the build deliberately did not fetch (an animation id this client
        # cannot ask for). Its keys are absent, not corrupt; the converter
        # drops such sequences and says so.
        return []
    if o + n * sz > len(buf):
        raise ValueError("array %d x %s at %d overruns %d bytes"
                         % (n, fmt, o, len(buf)))
    if fmt == "<c":
        return buf[o:o + n]
    if fmt == "<48s":
        return [buf[o + i * 48:o + i * 48 + 48] for i in range(n)]
    one = len(struct.unpack_from(fmt, buf, 0)) if False else None
    out = []
    unpack = struct.Struct(fmt).unpack_from
    for i in range(n):
        v = unpack(buf, o + i * sz)
        out.append(v if len(v) > 1 else v[0])
    return out


# Keyframe arrays retail says are in an .anim file that the file is too short
# to hold. Measured on Pandaren male: bones 136-138 of sequences 74, 75, 77 and
# 123 end up to 296 bytes past the end of their AFM2 payload - the facial bones
# were added after those files were cut. Such an array has no keys for that
# sequence (the bone holds its rest pose), and every one is recorded here so
# the converter can report it instead of reading past a buffer.
SHORT_ANIM: list[tuple] = []


def _ext_arr(buf, n, off, fmt, src, s):
    if buf is src.main:
        return _arr(buf, n, off, fmt)
    try:
        return _arr(buf, n, off, fmt)
    except ValueError:
        SHORT_ANIM.append((s, n, off, len(buf)))
        return []


def _track(src: Source, buf, o, vfmt, with_values=True, nseq=None):
    interp, gseq = struct.unpack_from("<Hh", buf, o)
    tn, to = struct.unpack_from("<II", buf, o + 4)
    times, values = [], []
    # the outer arrays always live in the record's own buffer
    for s in range(tn):
        n, off = struct.unpack_from("<II", buf, to + s * 8)
        b = src.seq_buf(s) if gseq < 0 else src.main
        times.append(_ext_arr(b, n, off, "<I", src, s) if n else [])
    if with_values:
        vn, vo = struct.unpack_from("<II", buf, o + 12)
        for s in range(vn):
            n, off = struct.unpack_from("<II", buf, vo + s * 8)
            b = src.seq_buf(s) if gseq < 0 else src.main
            values.append(_ext_arr(b, n, off, VAL[vfmt], src, s) if n else [])
    return Track(interp, gseq, times, values, vfmt)


def read_record(src: Source, buf, o, kind):
    if isinstance(kind, str) and kind.startswith("<"):
        raise TypeError
    rec = {}
    for f in SCHEMA[kind]:
        t, name = f[0], f[1]
        if t == "u32":
            rec[name] = struct.unpack_from("<I", buf, o)[0]; o += 4
        elif t == "i32":
            rec[name] = struct.unpack_from("<i", buf, o)[0]; o += 4
        elif t == "f32":
            rec[name] = struct.unpack_from("<f", buf, o)[0]; o += 4
        elif t == "u16":
            rec[name] = struct.unpack_from("<H", buf, o)[0]; o += 2
        elif t == "i16":
            rec[name] = struct.unpack_from("<h", buf, o)[0]; o += 2
        elif t == "raw":
            sz = struct.calcsize(f[2])
            rec[name] = struct.unpack_from(f[2], buf, o); o += sz
        elif t == "arr":
            n, off = struct.unpack_from("<II", buf, o)
            rec[name] = _arr(buf, n, off, f[2]); o += 8
        elif t == "track":
            rec[name] = _track(src, buf, o, f[2]); o += 20
        elif t == "trackbase":
            rec[name] = _track(src, buf, o, None, with_values=False); o += 12
    return rec


class Model:
    """Every header array of an M2, as lists. `hdr` keeps the scalar fields
    (version, flags, bounds) as raw bytes of the header."""

    def __init__(self):
        self.version = 264
        self.flags = 0
        self.nviews = 1
        self.bounds = b"\0" * (0xD8 - 0xA0)   # bbox, radius, collision bbox, radius
        self.a: dict[str, list] = {}

    # sequence helpers -----------------------------------------------------
    def seq_field(self, i, k):
        names = ("id", "var", "dur", "speed", "flags", "freq", "pad",
                 "rmin", "rmax", "blend")
        return self.a["seqs"][i]["data"][names.index(k)]


def read(md20: bytes, ext=None) -> Model:
    """Parse an MD20 block. `ext(seqIndex)` returns the .anim payload for an
    external sequence (or None)."""
    m = Model()
    m.version = struct.unpack_from("<I", md20, 4)[0]
    m.flags = struct.unpack_from("<I", md20, 0x10)[0]
    m.nviews = struct.unpack_from("<I", md20, 0x44)[0]
    m.bounds = md20[0xA0:0xD8]
    # An alias (flag 0x40) carries its target's (count, offset) pairs, so its
    # keyframes are read out of whatever buffer the TARGET's are in.
    target = alias_targets(md20)
    src = Source(md20, None if ext is None else (lambda s: ext(target.get(s, s))))
    if m.flags & 0x8:
        n, o = struct.unpack_from("<II", md20, 0x130)
        m.combiner_combos = _arr(md20, n, o, "<H")
    for off, name, kind in HEADER:
        n, o = struct.unpack_from("<II", md20, off)
        if kind is None:
            if n:
                raise ValueError("%s present (%d) - not supported here" % (name, n))
            m.a[name] = []
            continue
        if isinstance(kind, str) and kind.startswith("<"):
            m.a[name] = _arr(md20, n, o, kind)
        else:
            if kind == "camera" and m.version >= 272:
                kind = "camera274"
            sz = rec_size(kind)
            m.a[name] = [read_record(src, md20, o + i * sz, kind) for i in range(n)]
    return m


def read_array(buf: bytes, at: int, kind, src: Source, version=264):
    """One M2Array whose (count, offset) pair sits at `at` in `buf` - the way a
    .skel chunk stores the arrays the model header would otherwise hold."""
    n, o = struct.unpack_from("<II", buf, at)
    if isinstance(kind, str) and kind.startswith("<"):
        return _arr(buf, n, o, kind)
    if kind == "camera" and version >= 272:
        kind = "camera274"
    sz = rec_size(kind)
    return [read_record(src, buf, o + i * sz, kind) for i in range(n)]


def read_retail(m2: bytes, skel: bytes | None, anim) -> Model:
    """A retail character body: MD21 plus, when it has one, its .skel.

    `anim(fdid)` returns the bytes of a retail .anim file. Retail chunks those:
    AFM2 holds the keyframes of the MODEL's own tracks, AFSB the skeleton's
    bones and AFSA the skeleton's attachments, each addressed from offset 0 of
    its own payload - which is exactly what a 3.3.5 .anim is for one buffer.
    """
    import retail_m2 as RM
    c = RM.chunks(m2)
    md = c["MD21"][0]
    sk = RM.chunks(skel) if skel else None

    # sequences decide which keyframes are external, so read them first
    if sk and "SKS1" in sk:
        sks = sk["SKS1"][0]
        seqbuf, seq_at = sks, 8
    else:
        seqbuf, seq_at = md, 0x1C
    n, o = struct.unpack_from("<II", seqbuf, seq_at)
    seqs = [struct.unpack_from("<HHIfI", seqbuf, o + i * 64) for i in range(n)]
    flags = [s[4] for s in seqs]
    ali = [struct.unpack_from("<H", seqbuf, o + i * 64 + 62)[0] for i in range(n)]

    def resolve(s):
        j, hops = s, 0
        while j < n and flags[j] & 0x40 and hops < n:
            j = ali[j]
            hops += 1
        return j

    afid_src = (sk.get("AFID") if sk else None) or c.get("AFID")
    by_id = {}
    if afid_src:
        for aid, sub, fid in RM.afid(afid_src[0]):
            by_id[(aid, sub)] = fid
    cache = {}

    def payload(s, which):
        s = resolve(s)
        if s >= n or flags[s] & 0x20:
            return None                     # in the container itself
        key = (seqs[s][0], seqs[s][1])
        fid = by_id.get(key)
        if not fid:
            return b""                      # external with no file: no keys
        if fid not in cache:
            d = anim(fid)
            cache[fid] = RM.chunks(d) if d else {}
        ch = cache[fid]
        return ch.get(which, [b""])[0]

    m = read(md, ext=lambda s: payload(s, "AFM2"))
    if sk:
        if "SKS1" in sk:
            src = Source(sks, None)
            m.a["gloops"] = read_array(sks, 0, "<I", src)
            m.a["seqs"] = read_array(sks, 8, "sequence", src)
            m.a["seqlookup"] = read_array(sks, 16, "<h", src)
        if "SKB1" in sk:
            skb = sk["SKB1"][0]
            src = Source(skb, lambda s: payload(s, "AFSB"))
            m.a["bones"] = read_array(skb, 0, "bone", src)
            m.a["keybones"] = read_array(skb, 8, "<h", src)
        if "SKA1" in sk:
            ska = sk["SKA1"][0]
            src = Source(ska, lambda s: payload(s, "AFSA"))
            m.a["attach"] = read_array(ska, 0, "attachment", src)
            m.a["attachlookup"] = read_array(ska, 8, "<h", src)
    m.retail_textures = RM.u32s(c["TXID"][0]) if "TXID" in c else []
    if sk and "SKPD" in sk:
        merge_parent_skeleton(m, c, sk, anim)
    return m


# ---------------------------------------------------------------------------
# a CHILD skeleton's animations live in its parent (SKPD)
# ---------------------------------------------------------------------------
# Measured on every chained retail body (2026-10-07: Nightborne, Highmountain,
# Void Elf, Lightforged, Dark Iron, Mechagnome): the child .skel overrides a
# handful of sequences (13..107) and the parent .skel - often the base race's
# own (Highmountain's is Tauren's 1839011) - holds the rest (341..402). Child
# and parent bones match one to one (same key, parent and name hash). Retail's
# rule, and the RaceGuide's working Highmountain/Mechagnome ports: the parent's
# sequence list, each child sequence replacing its (id, variation), any
# child-only sequence appended. A sequence's keyframes come from ITS skeleton's
# own buffers and .anim files (its own AFID), aliases are resolved inside that
# skeleton, and the model carries what an inherited sequence needs of it:
#   PSBC  this model's bounds for each parent sequence (count, offset 16,
#         28 bytes: box min, box max, radius)
#   PEDC  this model's event timestamps for each parent sequence (count =
#         the model's events, table at 16: interp u16, gseq i16, n u32,
#         arrays u32; arrays: n x (count u32, offset u32) of u32 times)
def _skel_level(sk: dict, anim) -> dict:
    """One skeleton's own sequences, bones and attachments, keyframes read from
    its own buffers and .anim files."""
    import retail_m2 as RM
    sks = sk["SKS1"][0]
    n, o = struct.unpack_from("<II", sks, 8)
    raw = [struct.unpack_from("<HHIfI", sks, o + i * 64) for i in range(n)]
    flags = [r[4] for r in raw]
    ali = [struct.unpack_from("<H", sks, o + i * 64 + 62)[0] for i in range(n)]
    by_id = {}
    if "AFID" in sk:
        for aid, sub, fid in RM.afid(sk["AFID"][0]):
            by_id[(aid, sub)] = fid
    cache = {}

    def resolve(i):
        j, hops = i, 0
        while j < n and flags[j] & 0x40 and hops < n:
            j = ali[j]
            hops += 1
        return j

    def payload(i, which):
        i = resolve(i)
        if i >= n or flags[i] & 0x20:
            return None
        fid = by_id.get((raw[i][0], raw[i][1]))
        if not fid:
            return b""
        if fid not in cache:
            d = anim(fid)
            cache[fid] = RM.chunks(d) if d else {}
        return cache[fid].get(which, [b""])[0]

    src = Source(sks, None)
    lv = {"seqs": read_array(sks, 8, "sequence", src), "gloops": read_array(sks, 0, "<I", src),
          "resolve": resolve}
    skb = sk["SKB1"][0]
    lv["bones"] = read_array(skb, 0, "bone", Source(skb, lambda i: payload(i, "AFSB")))
    lv["keybones"] = read_array(skb, 8, "<h", Source(skb, None))
    if "SKA1" in sk:
        ska = sk["SKA1"][0]
        lv["attach"] = read_array(ska, 0, "attachment", Source(ska, lambda i: payload(i, "AFSA")))
    return lv


def merge_parent_skeleton(m: "Model", c: dict, sk: dict, anim) -> None:
    import retail_m2 as RM
    parent_fid = struct.unpack_from("<I", sk["SKPD"][0], 8)[0]
    pd = anim(parent_fid)
    if not pd:
        raise RuntimeError("parent skeleton %d is not fetched" % parent_fid)
    psk = RM.chunks(pd)
    if "SKPD" in psk:
        raise RuntimeError("parent skeleton %d has a parent of its own: a chain no "
                           "retail body uses yet - refusing rather than guessing" % parent_fid)
    child, par = _skel_level(sk, anim), _skel_level(psk, anim)
    if len(child["bones"]) != len(par["bones"]):
        raise RuntimeError("child %d / parent %d bones" % (len(child["bones"]), len(par["bones"])))
    for a, b in zip(child["bones"], par["bones"]):
        if (a["key"], a["parent"], a["crc"]) != (b["key"], b["parent"], b["crc"]):
            raise RuntimeError("child and parent skeletons differ in structure")
    # the parent's global loops are a prefix of the child's (measured on every
    # chained body): a parent track's loop index means the same in the child
    if child["gloops"][:len(par["gloops"])] != par["gloops"]:
        raise RuntimeError("the parent's global loops are not a prefix of the child's")
    # the merged list: parent order, child overrides by (id, variation)
    order = [("p", i) for i in range(len(par["seqs"]))]
    at = {(s["data"][0], s["data"][1]): k for k, s in enumerate(par["seqs"])}
    for i, s in enumerate(child["seqs"]):
        key = (s["data"][0], s["data"][1])
        if key in at:
            order[at[key]] = ("c", i)
        else:
            order.append(("c", i))
    lv = {"c": child, "p": par}
    # this model's own data for parent sequences
    psbc = c.get("PSBC", [None])[0]
    pedc = c.get("PEDC", [None])[0]
    bounds = None
    if psbc:
        bn, bo = struct.unpack_from("<II", psbc, 0)
        if bn != len(par["seqs"]):
            raise RuntimeError("PSBC has %d bounds for %d parent sequences" % (bn, len(par["seqs"])))
        bounds = [struct.unpack_from("<7f", psbc, bo + i * 28) for i in range(bn)]
    pev = None
    if pedc:
        en, eo = struct.unpack_from("<II", pedc, 0)
        if en != len(m.a["events"]):
            raise RuntimeError("PEDC has %d event tracks for %d events" % (en, len(m.a["events"])))
        pev = []
        for e in range(en):
            interp, gseq, ns, arr = struct.unpack_from("<HhII", pedc, eo + e * 12)
            if ns != len(par["seqs"]) or gseq != -1:
                raise RuntimeError("PEDC event %d: %d arrays, gseq %d" % (e, ns, gseq))
            times = []
            for i in range(ns):
                cnt, pos = struct.unpack_from("<II", pedc, arr + i * 8)
                times.append(list(struct.unpack_from("<%dI" % cnt, pedc, pos)))
            pev.append(times)

    def pick(level, i):
        """(level dict, concrete index): an alias resolved inside its skeleton."""
        L = lv[level]
        return L, L["resolve"](i)

    merged_at = {}
    for k, (level, i) in enumerate(order):
        d = lv[level]["seqs"][i]["data"]
        merged_at[(d[0], d[1])] = k
    seqs = []
    for level, i in order:
        rec = list(lv[level]["seqs"][i]["data"])
        # variationNext names a sequence of ITS skeleton: re-point it at the
        # merged entry of the same (id, variation), so the client's random
        # variation chains (idles, attacks) stay retail's
        vn = rec[17]
        if 0 <= vn < len(lv[level]["seqs"]):
            t = lv[level]["seqs"][vn]["data"]
            rec[17] = merged_at.get((t[0], t[1]), -1)
        else:
            rec[17] = -1
        # aliases become their target's keys; a sequence keeps the in-model
        # flag (0x20) the skeleton it comes from gives it (its target's, for an
        # alias). Clearing it made EVERY sequence of a child-skeleton body an
        # external .anim, Stand included - and the client draws Stand while an
        # .anim streams in: it read Stand's unloaded keys (null) and crashed
        # (0x00828565, 2026-10-07, Void Elf / Lightforged / Mechagnome...)
        src = lv[level]["seqs"][lv[level]["resolve"](i)]["data"][4]
        rec[4] = (rec[4] & ~0x60) | (src & 0x20)
        rec[18] = 0
        if level == "p" and bounds:
            rec[10:17] = bounds[i]
        seqs.append({"data": tuple(rec)})

    def merged_track(tracks_by_level, name):
        base = tracks_by_level["c"]
        times, values = [], []
        for level, i in order:
            t = tracks_by_level[level]
            j = lv[level]["resolve"](i)
            times.append(list(t.times[j]) if j < len(t.times) else [])
            if t.values is not None:
                values.append(list(t.values[j]) if j < len(t.values) else [])
        interp = base.interp
        if not any(base.times) and any(tracks_by_level["p"].times):
            interp = tracks_by_level["p"].interp
        return Track(interp, base.gseq, times, values if base.values is not None else None, base.fmt)

    def keys(t):
        return sum(len(x) for x in t.times)

    def merge_records(name, fields):
        """A track is global (a loop clock) or per sequence. Where child and
        parent agree it is per sequence, the sequences are merged. Where one is
        global, measured on every chained body the other has no keys at all:
        the global one IS the bone's animation (a child's own jiggle, or the
        parent's loop the child never redefined) and is kept as it is."""
        out = []
        for k, rec in enumerate(child[name]):
            r = dict(rec)
            for f in fields:
                tc, tp = rec[f], par[name][k][f]
                if tc.gseq < 0 and tp.gseq < 0:
                    r[f] = merged_track({"c": tc, "p": tp}, f)
                elif tc.gseq >= 0 and (tp.gseq >= 0 or not keys(tp)):
                    r[f] = tc                    # the child's own loop
                elif tp.gseq >= 0 and not keys(tc):
                    r[f] = tp                    # the parent's loop, never redefined
                else:
                    raise RuntimeError("%s %d %s: global in one skeleton and keyed per "
                                       "sequence in the other" % (name, k, f))
            out.append(r)
        return out

    m.a["bones"] = merge_records("bones", ("trans", "rot", "scale"))
    m.a["keybones"] = child["keybones"]
    if "attach" in child and "attach" in par and len(child["attach"]) == len(par["attach"]):
        m.a["attach"] = merge_records("attach", ("animate",))
    m.a["gloops"] = child["gloops"]
    m.a["seqs"] = seqs
    # events: a child sequence keeps the model's own keys, a parent one PEDC's
    for e, ev in enumerate(m.a["events"]):
        t = ev["enabled"]
        own = t.times
        times = []
        for level, i in order:
            if level == "c":
                j = child["resolve"](i)
                times.append(list(own[j]) if j < len(own) else [])
            else:
                j = par["resolve"](i)
                times.append(list(pev[e][j]) if pev else [])
        t.times = times
    m.parent_skeleton = parent_fid


# ---------------------------------------------------------------------------
# writing
# ---------------------------------------------------------------------------
class Writer:
    def __init__(self):
        self.buf = bytearray()

    def align(self, a=16):
        while len(self.buf) % a:
            self.buf += b"\0"

    def put(self, data: bytes, align=16) -> int:
        self.align(align)
        o = len(self.buf)
        self.buf += data
        return o


def _pack_arr(items, fmt) -> bytes:
    if fmt == "<c":
        return bytes(items)
    if fmt == "<48s":
        return b"".join(items)
    if fmt == "<h":
        # 16-bit lookup tables are read as signed (-1 = none) but some hold
        # the unsigned 0xFFFF the client means by "environment mapped"; both
        # are the same two bytes.
        items = [x - 0x10000 if x > 0x7FFF else x for x in items]
    s = struct.Struct(fmt)
    return b"".join(s.pack(*(x if isinstance(x, tuple) else (x,))) for x in items)


def write(m: Model, external: set[int] | None = None):
    """Serialize `m` as version 264. Sequences in `external` get their
    keyframes written to per-sequence .anim buffers instead of the model.

    Returns (md20 bytes, {seqIndex: anim bytes})."""
    external = external or set()
    targets = seq_alias_info(m.a.get("seqs", []))
    w = Writer()
    # Global flag 0x8 makes the header 8 bytes longer: texture_combiner_combos
    # at 0x130, which a two-layer batch needs (m2_port.py, ComputeShaderIds).
    combos = getattr(m, "combiner_combos", None) or []
    long_header = bool(m.flags & 0x8)
    w.put(b"\0" * (HEADER_SIZE + (8 if long_header else 0)), 1)
    anims = {s: Writer() for s in external}

    def put_nested(data: bytes, seq, gseq):
        """Write a per-sequence keyframe array; returns its offset in the
        buffer it went to."""
        if seq is not None and gseq < 0 and seq in external:
            return anims[seq].put(data, 4)
        return w.put(data, 4)

    def write_track(tr: Track, at: int, values=True):
        struct.pack_into("<Hh", w.buf, at, tr.interp, tr.gseq)
        # outer arrays: one (count, offset) pair per sequence. An alias shares
        # its target's pair - the client reads it out of the target's buffer -
        # so targets are written first.
        def outer(lists, fmt):
            pairs = [(0, 0)] * len(lists)
            order = [s for s in range(len(lists)) if s not in targets] + \
                    [s for s in range(len(lists)) if s in targets]
            for s in order:
                items = lists[s]
                if not items:
                    continue
                t = targets.get(s) if tr.gseq < 0 else None
                if t is not None and t < len(lists) and lists[t] == items:
                    pairs[s] = pairs[t]
                    continue
                data = _pack_arr(items, fmt)
                pairs[s] = (len(items), put_nested(data, s if t is None else t,
                                                   tr.gseq))
            if not lists:
                return 0, 0
            o = w.put(b"".join(struct.pack("<II", *p) for p in pairs), 4)
            return len(lists), o
        n, o = outer(tr.times, "<I")
        struct.pack_into("<II", w.buf, at + 4, n, o)
        if values:
            n, o = outer(tr.values, VAL[tr.fmt])
            struct.pack_into("<II", w.buf, at + 12, n, o)

    def write_records(name, kind, recs) -> tuple[int, int]:
        sz = rec_size(kind)
        base = w.put(b"\0" * (sz * len(recs)), 16) if recs else 0
        for i, rec in enumerate(recs):
            o = base + i * sz
            for f in SCHEMA[kind]:
                t, fname = f[0], f[1]
                v = rec[fname]
                if t == "u32":
                    struct.pack_into("<I", w.buf, o, v); o += 4
                elif t == "i32":
                    struct.pack_into("<i", w.buf, o, v); o += 4
                elif t == "f32":
                    struct.pack_into("<f", w.buf, o, v); o += 4
                elif t == "u16":
                    struct.pack_into("<H", w.buf, o, v); o += 2
                elif t == "i16":
                    struct.pack_into("<h", w.buf, o, v); o += 2
                elif t == "raw":
                    struct.pack_into(f[2], w.buf, o, *v)
                    o += struct.calcsize(f[2])
                elif t == "arr":
                    if len(v):
                        no = w.put(_pack_arr(v, f[2]), 4)
                        struct.pack_into("<II", w.buf, o, len(v), no)
                    o += 8
                elif t == "track":
                    write_track(v, o); o += 20
                elif t == "trackbase":
                    write_track(v, o, values=False); o += 12
        return len(recs), base

    for off, name, kind in HEADER:
        items = m.a.get(name, [])
        if kind is None or not items:
            struct.pack_into("<II", w.buf, off, 0, 0)
            continue
        if isinstance(kind, str) and kind.startswith("<"):
            o = w.put(_pack_arr(items, kind), 16)
            struct.pack_into("<II", w.buf, off, len(items), o)
        else:
            n, o = write_records(name, kind, items)
            struct.pack_into("<II", w.buf, off, n, o)

    if long_header:
        if not combos:
            raise ValueError("global flag 0x8 set but no texture_combiner_combos")
        o = w.put(_pack_arr(combos, "<H"), 16)
        struct.pack_into("<II", w.buf, 0x130, len(combos), o)
    w.buf[0:4] = b"MD20"
    struct.pack_into("<I", w.buf, 4, m.version)
    struct.pack_into("<I", w.buf, 0x10, m.flags)
    struct.pack_into("<I", w.buf, 0x44, m.nviews)
    w.buf[0xA0:0xD8] = m.bounds
    w.align(16)
    return bytes(w.buf), {s: bytes(a.buf) for s, a in anims.items()}


# ---------------------------------------------------------------------------
# round trip
# ---------------------------------------------------------------------------
def equal_models(a: Model, b: Model) -> list[str]:
    diffs = []
    for k in ("version", "flags", "nviews", "bounds"):
        if getattr(a, k) != getattr(b, k):
            diffs.append(k)
    for _off, name, _kind in HEADER:
        x, y = a.a.get(name, []), b.a.get(name, [])
        if len(x) != len(y):
            diffs.append("%s count %d != %d" % (name, len(x), len(y)))
            continue
        for i, (p, q) in enumerate(zip(x, y)):
            if p != q:
                diffs.append("%s[%d]" % (name, i))
                break
    return diffs


def seq_alias_info(seqs: list) -> dict[int, int]:
    """alias sequence index -> the sequence whose keyframes it plays, for the
    `seqs` records of a Model (follows aliasNext while 0x40 is set)."""
    out = {}
    for i, s in enumerate(seqs):
        fl, ali = s["data"][4], s["data"][-1]
        j, hops = i, 0
        while seqs[j]["data"][4] & 0x40 and hops < len(seqs):
            j = seqs[j]["data"][-1]
            hops += 1
        if j != i:
            out[i] = j
    return out


def alias_targets(md20: bytes) -> dict[int, int]:
    n, o = struct.unpack_from("<II", md20, 0x1C)
    flags = [struct.unpack_from("<I", md20, o + i * 64 + 12)[0] for i in range(n)]
    ali = [struct.unpack_from("<H", md20, o + i * 64 + 62)[0] for i in range(n)]
    out = {}
    for i in range(n):
        j, hops = i, 0
        while flags[j] & 0x40 and hops < n and ali[j] < n:
            j = ali[j]
            hops += 1
        if j != i:
            out[i] = j
    return out


def external_set(md20: bytes) -> list[tuple[int, int, int]]:
    """(seqIndex, animId, variation) of every sequence whose keyframes are not
    in the model: flag 0x20 clear and not an alias (0x40)."""
    n, o = struct.unpack_from("<II", md20, 0x1C)
    out = []
    for i in range(n):
        aid, var, _d, _s, fl = struct.unpack_from("<HHIfI", md20, o + i * 64)
        if not (fl & 0x20) and not (fl & 0x40):
            out.append((i, aid, var))
    return out


def anim_name(model_path: str, aid: int, var: int) -> str:
    """The 3.3.5 client's external animation file name for a model."""
    base = model_path[:-3] if model_path.lower().endswith(".m2") else model_path
    return "%s%04d-%02d.anim" % (base, aid, var)


if __name__ == "__main__":
    raise SystemExit("library module: run `python bp.py selftest` instead")
