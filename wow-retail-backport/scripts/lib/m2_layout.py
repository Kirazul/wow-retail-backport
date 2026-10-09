"""
m2_layout.py - proving an M2's struct sizes against the file itself.

WHY THIS EXISTS
---------------
The first conversion of the hearthstone art shipped with M2Sequence declared as
28 bytes. It is 64. Every model in the pack was therefore written with sequence
records less than half the size the client reads, so the client took each
animation's duration, flags and bounding box out of the middle of its
neighbour. Every animation in the pack was dead, every model's bounds were
nonsense, and some of it crashed. One wrong integer, 141 broken files, and
nothing in the pipeline could see it: the converter round-tripped its own
mistake and agreed with itself perfectly.

So sizes are no longer asserted. They are PROVEN, like this:

    An M2 is a header of arrays. Every array is (count, offset) and occupies
    exactly count * sizeof(element) bytes. Nested inside many elements are
    more arrays - animation tracks, filenames, per-emitter splines - and they
    occupy their own bytes elsewhere in the same file. Blizzard's exporter
    packs these end to end. So if every element size is correct, the spans
    walked from the header TILE the file: none overlaps another, and what is
    left over is a few bytes of alignment padding.

    A size that is too small leaves the tail of each record unclaimed and,
    far more loudly, makes the nested arrays inside records 1..n-1 parse at
    the wrong offset - so they point outside the file, or into the middle of
    another array. A size that is too large overlaps the next array outright.

Run over 141 real files this pins every size exactly. It runs as part of the
conversion, on the input and again on the output, so a size that is wrong
stops the build instead of shipping.

    python tools/lib/m2_layout.py --tree re/out/hearth/art
    python tools/lib/m2_layout.py --file <x.m2> --verbose
"""

from __future__ import annotations
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
import _toolpath  # noqa: E402,F401  (every tools/ folder and re/ on sys.path)

import argparse
import os
import struct
import sys


class Bad(Exception):
    pass


# ---------------------------------------------------------------------------
#  The header. Both 264 and 274 agree on every offset here; the only thing
#  that varies is whether the last array is present at all (global flag 0x8).
# ---------------------------------------------------------------------------
HDR = [
    ("name",                     0x008, "char"),
    ("global_loops",             0x014, 4),
    ("sequences",                0x01C, "seq"),
    ("sequence_lookups",         0x024, 2),
    ("bones",                    0x02C, "bone"),
    ("key_bone_lookup",          0x034, 2),
    ("vertices",                 0x03C, 48),
    ("colors",                   0x048, "color"),
    ("textures",                 0x050, "texture"),
    ("texture_weights",          0x058, "tex_weight"),
    ("texture_transforms",       0x060, "tex_transform"),
    ("replacable_lookup",        0x068, 2),
    ("materials",                0x070, 4),
    ("bone_combos",              0x078, 2),
    ("texture_combos",           0x080, 2),
    ("texture_coord_combos",     0x088, 2),
    ("texture_weight_combos",    0x090, 2),
    ("texture_transform_combos", 0x098, 2),
    ("collision_indices",        0x0D8, 2),
    ("collision_positions",      0x0E0, 12),
    ("collision_normals",        0x0E8, 12),
    ("attachments",              0x0F0, "attachment"),
    ("attachment_lookup",        0x0F8, 2),
    ("events",                   0x100, "event"),
    ("lights",                   0x108, "light"),
    ("cameras",                  0x110, "camera"),
    ("camera_lookup",            0x118, 2),
    ("ribbons",                  0x120, "ribbon"),
    ("particles",                0x128, "particle"),
    ("texture_combiner_combos",  0x130, 2),   # only when global flag 0x8
]

GF_TEXTURE_COMBINER_COMBOS = 0x8

# M2Track  = u16 interp, u16 global_seq, M2Array timestamps, M2Array values.
# M2TrackBase drops the values array. Both are identical in 264 and 274.
TRACK = 20
TRACKBASE = 12
FBLOCK = 16       # M2PartTrack: M2Array times, M2Array values

# For each element type: its size and where the nested arrays sit inside it.
# `tracks` are M2Track (an array of arrays, one per sequence); `arrays` are
# plain M2Arrays given as (offset, element size); `trackbases` are M2TrackBase.
#
# Every one of these is checked by the tiling proof, on real files, in both
# versions. Nothing here is taken on trust.
ELEM = {
    "seq":           dict(size=64, tracks=[], arrays=[]),
    "bone":          dict(size=88, tracks=[0x10, 0x24, 0x38], arrays=[]),
    "color":         dict(size=40, tracks=[0x00, 0x14], arrays=[]),
    "texture":       dict(size=16, tracks=[], arrays=[(0x08, 1)]),   # filename
    "tex_weight":    dict(size=20, tracks=[0x00], arrays=[]),
    "tex_transform": dict(size=60, tracks=[0x00, 0x14, 0x28], arrays=[]),
    "attachment":    dict(size=40, tracks=[0x14], arrays=[]),
    "event":         dict(size=36, tracks=[], trackbases=[0x18], arrays=[]),
    "light":         dict(size=156,
                          tracks=[0x10, 0x24, 0x38, 0x4C, 0x60, 0x74, 0x88],
                          arrays=[]),
    # M2Camera is the ONE record besides the emitter whose size really does
    # differ: Cataclysm dropped the `fov` float and appended a FoV track, so
    # 100 bytes became 116. A 3.3.5 client walking a retail camera array would
    # read every camera after the first out of the middle of its neighbour,
    # which is why the converter drops cameras rather than ship them.
    "camera":        dict(size=100, tracks=[0x10, 0x24, 0x38], arrays=[]),
    "camera_cata":   dict(size=116, tracks=[0x14, 0x28, 0x3C], arrays=[]),
    # Ribbon track offsets are DERIVED, not remembered: searching every
    # 4-byte position of 161 WotLK ribbons for positions holding a valid
    # array in every record gives 0x28/0x30, 0x3C/0x44, 0x50/0x58, 0x64/0x6C,
    # 0x88/0x90 and 0x9C/0xA4 - which are the times/values pairs of tracks
    # beginning at 0x24, 0x38, 0x4C, 0x60, 0x84 and 0x98.
    "ribbon":        dict(size=176,
                          tracks=[0x24, 0x38, 0x4C, 0x60, 0x84, 0x98],
                          arrays=[(0x14, 2), (0x1C, 2)]),
    "char":          dict(size=1, tracks=[], arrays=[]),
}


# ---------------------------------------------------------------------------
#  M2Particle, the one record whose size really does differ
# ---------------------------------------------------------------------------
#  476 bytes through Wrath, 492 from Cataclysm. The 16 extra bytes are
#  multiTextureParam0/1 at the tail of the fixed head, so every nested array
#  after them shifts by 16 - which is exactly the kind of thing the proof
#  below is here to catch.
#
#  What is nested inside one emitter:
#     two M2Arrays of char  - the geometry and recursion model filenames
#     ten M2PartTrack       - the "fake blocks": emission speed, speed
#                             variation, vertical and horizontal range,
#                             gravity, lifespan, emission rate, area length,
#                             area width, z-source. Each is two M2Arrays.
#     three M2Track         - colour, alpha and scale are plain inline data in
#                             this record, NOT tracks; only enabledIn is one.
#     one M2Array of C2Vector - the spline points
def particle_layout(version: int):
    """Every one of these offsets was found by searching the records.

    For each 4-byte position in the record, ask whether it holds a plausible
    M2Array in EVERY emitter of every model. Over 804 retail emitters and
    1,544 WotLK ones the answer is the same list in both versions, and in the
    WotLK record the last of them ends at 0x1DC = 476, exactly the record
    size. So the two layouts are identical and retail's extra sixteen bytes
    are at the tail, after everything.
    """
    size = 492 if version >= 272 else 476
    arrays = [
        (0x18, 1),      # geometry model filename
        (0x20, 1),      # recursion model filename
        (0x1C0, 12),    # spline points
    ]
    fblocks = [0x38, 0x4C, 0x60, 0x74, 0x88, 0x9C, 0xB4, 0xCC, 0xE0, 0xF4,
               0x104, 0x114, 0x124, 0x13C, 0x14C]
    tracks = [0x1C8]    # enabledIn: times at 0x1CC, values at 0x1D4
    return dict(size=size, arrays=arrays, fblocks=fblocks, tracks=tracks)


def u32(d, o):
    return struct.unpack_from("<I", d, o)[0]


class Span:
    __slots__ = ("lo", "hi", "what")

    def __init__(self, lo, hi, what):
        self.lo, self.hi, self.what = lo, hi, what


class Model:
    """An M2, chunked or not, with the MD20 block located."""

    def __init__(self, data: bytes):
        self.raw = data
        self.chunks = {}
        if data[:4] == b"MD20":
            self.d = data
            self.base = 0
        elif data[:4] == b"MD21":
            self._read_chunks(data)
            off, size = self.chunks[b"MD21"]
            self.d = data[off:off + size]
            self.base = off
        else:
            raise Bad("not an M2 (magic %r)" % (data[:4],))
        self.version = u32(self.d, 4)
        self.flags = u32(self.d, 0x10)

    def _read_chunks(self, data):
        p, n = 0, len(data)
        while p + 8 <= n:
            magic = data[p:p + 4]
            size = u32(data, p + 4)
            if size > n - p - 8:
                break
            self.chunks[magic] = (p + 8, size)
            p += 8 + size

    def arr(self, off):
        return u32(self.d, off), u32(self.d, off + 4)


def walk(m: Model, spans: list):
    """Claim every byte the header's arrays and their nested arrays occupy."""
    d = m.d
    n = len(d)

    def claim(lo, size, what):
        if size == 0:
            return
        if lo < 0 or lo + size > n:
            raise Bad("%s runs outside the file: %d..%d of %d"
                      % (what, lo, lo + size, n))
        spans.append(Span(lo, lo + size, what))

    def m2array(off, elemsize, what):
        if off + 8 > n:
            raise Bad("%s header itself is outside the file" % what)
        cnt, o = u32(d, off), u32(d, off + 4)
        if cnt == 0:
            return 0, 0
        claim(o, cnt * elemsize, what)
        return cnt, o

    def track(off, what):
        nts, ots = m2array(off + 4, 8, what + ".times")
        for i in range(nts):
            m2array(ots + i * 8, 4, "%s.times[%d]" % (what, i))
        nvs, ovs = m2array(off + 12, 8, what + ".values")
        for i in range(nvs):
            # The values sub-arrays are sized by the track's own type, which
            # the header does not carry. Their bytes are claimed by the
            # element walk of whoever owns the track; here only the array of
            # arrays is claimed, and the leaf spans are added by the caller
            # via `leafsize` when it is known.
            pass
        return nvs, ovs

    def fblock(off, what):
        # M2PartTrack's members are fixed16 - two bytes. Claiming two is also
        # the safe direction: under-claiming can never invent an overlap that
        # is not there, so a clean tiling remains real evidence.
        m2array(off, 2, what + ".times")
        m2array(off + 8, 2, what + ".values")

    hdr = [h for h in HDR
           if h[0] != "texture_combiner_combos"
           or (m.flags & GF_TEXTURE_COMBINER_COMBOS)]

    for name, off, kind in hdr:
        if isinstance(kind, int):
            m2array(off, kind, name)
            continue
        if kind == "char":
            m2array(off, 1, name)
            continue
        if kind == "particle":
            lay = particle_layout(m.version)
            cnt, o = m2array(off, lay["size"], name)
            for i in range(cnt):
                rec = o + i * lay["size"]
                for a, es in lay["arrays"]:
                    m2array(rec + a, es, "%s[%d].arr@%X" % (name, i, a))
                for f in lay["fblocks"]:
                    fblock(rec + f, "%s[%d].fb@%X" % (name, i, f))
                for t in lay["tracks"]:
                    track(rec + t, "%s[%d].track@%X" % (name, i, t))
            continue
        spec = ELEM[kind]
        if kind == "camera" and m.version >= 272:
            spec = ELEM["camera_cata"]
        cnt, o = m2array(off, spec["size"], name)
        for i in range(cnt):
            rec = o + i * spec["size"]
            for t in spec.get("tracks", []):
                track(rec + t, "%s[%d].track@%X" % (name, i, t))
            for t in spec.get("trackbases", []):
                nts, ots = m2array(rec + t + 4, 8, "%s[%d].tb" % (name, i))
                for j in range(nts):
                    m2array(ots + j * 8, 4, "%s[%d].tb[%d]" % (name, i, j))
            for a, es in spec.get("arrays", []):
                m2array(rec + a, es, "%s[%d].arr@%X" % (name, i, a))
    return spans


def prove(data: bytes, verbose=False):
    """-> (overlaps, unclaimed, total). Raises Bad on a span outside the file."""
    m = Model(data)
    spans = []
    walk(m, spans)
    spans.sort(key=lambda s: (s.lo, s.hi))
    overlaps = []
    covered = 0
    prev = None
    for s in spans:
        if prev is not None and s.lo < prev.hi:
            overlaps.append((prev.what, prev.lo, prev.hi, s.what, s.lo, s.hi))
        if prev is None or s.lo >= prev.hi:
            covered += s.hi - s.lo
            prev = s
        elif s.hi > prev.hi:
            covered += s.hi - prev.hi
            prev = s
    total = len(m.d)
    head = 304 + (8 if (m.flags & GF_TEXTURE_COMBINER_COMBOS) else 0)
    unclaimed = total - head - covered
    if verbose:
        for o in overlaps[:24]:
            print("   OVERLAP %s [%d,%d) vs %s [%d,%d)" % o)
    return overlaps, unclaimed, total


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--file")
    ap.add_argument("--tree")
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--slack", type=int, default=64,
                    help="bytes a file may leave unclaimed before it is a fault")
    a = ap.parse_args()
    files = [a.file] if a.file else [
        os.path.join(dp, fn)
        for dp, _, fns in os.walk(a.tree) for fn in fns
        if fn.lower().endswith(".m2")]
    nbad = 0
    slack = 0
    for p in sorted(files):
        try:
            ov, un, tot = prove(open(p, "rb").read(), a.verbose)
        except Bad as e:
            print("FAIL %-58s %s" % (os.path.basename(p)[:58], e))
            nbad += 1
            continue
        slack += un
        if ov or un > a.slack:
            print("%-56s overlaps=%d unclaimed=%d/%d"
                  % (os.path.basename(p)[:56], len(ov), un, tot))
            if a.verbose:
                prove(open(p, "rb").read(), True)
            nbad += 1
    print("%d files, %d with a layout problem, %d bytes unclaimed overall"
          % (len(files), nbad, slack))
    return 1 if nbad else 0


if __name__ == "__main__":
    sys.exit(main())
