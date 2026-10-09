"""
verify_bounds.py - every cross-reference wow.exe reads WITHOUT checking it.

A 3.3.5 model is a pile of arrays that index each other. The client bounds
checks some of those reads and not others, and the ones it does not check are
where a converted model stops being a rendering problem and becomes an
access violation or a hang.

Every rule below names the instruction that does the unchecked read. Nothing
here is a format-document rule; if it is listed, the code was read.

UNCHECKED BY THE CLIENT - these are crashes:

  0x00836a3e  texture_combiner_combos[batch.shader_id + unit]
  0x00836a56  texture_coord_combos[batch.texCoordComboIndex + unit]
  0x008369c6  materials[batch.materialIndex]            (also 0x0081f1ef)
  0x00822120  bone_matrices[submesh.centerBoneIndex]
  0x00821ed2  texture_weight_combos[batch.texWeightComboIndex + unit]
  0x00826235  sequence_lookups[animId % count] used as a sequence index at
              0x0082624c before the range test at 0x00826293
  0x008262b6  sequence[i].variationNext walked to 0xFFFF with no bound and no
              step limit - an out-of-range link reads off the end, a cycle
              hangs the client

CHECKED BY THE CLIENT - listed so the difference is on the record:

  0x0081f4fc  texture_combos[...] against textures.count
  0x0081f5b7  transform index against texture_transforms.count
  0x00821ead  batch.colorIndex against colors.count

Used by: gates.py (bp.py audit)
"""
from __future__ import annotations
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
import _toolpath  # noqa: E402,F401  (puts scripts/lib on sys.path)
import collections, glob, os, struct, sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, HERE)
from mpq_read import MpqReader                          # noqa: E402


H = dict(gflags=0x010, globals=0x014, seq=0x01C, seqlookup=0x024,
         bones=0x02C, keybone=0x034, verts=0x03C, colors=0x048,
         tex=0x050, weights=0x058, transforms=0x060, replacable=0x068,
         mat=0x070, bonecombo=0x078, texcombo=0x080, coord=0x088,
         weightcombo=0x090, transcombo=0x098, attach=0x0F0,
         attachlookup=0x0F8, events=0x100, lights=0x108, cameras=0x110,
         cameralookup=0x118, ribbons=0x120, particles=0x128, combiner=0x130)

SZ = dict(seq=64, bone=88, color=40, tex=16, weight=20, transform=60,
          attach=40, event=36, light=156, ribbon=176, vertex=48,
          material=4, particle=476, batch=24, submesh=48)

# Where an M2Track sits inside each record, and how wide the record is. Track
# layout is u16 interp, i16 global_seq, M2Array times, M2Array values.
TRACKS = [
    ("bone.translation", "bones", "bone", 0x10),
    ("bone.rotation", "bones", "bone", 0x24),
    ("bone.scale", "bones", "bone", 0x38),
    ("color.rgb", "colors", "color", 0x00),
    ("color.alpha", "colors", "color", 0x14),
    ("weight", "weights", "weight", 0x00),
    ("transform.translation", "transforms", "transform", 0x00),
    ("transform.rotation", "transforms", "transform", 0x14),
    ("transform.scale", "transforms", "transform", 0x28),
    ("attach.animate", "attach", "attach", 0x14),
    ("light.ambient_color", "lights", "light", 0x10),
    ("light.ambient_intensity", "lights", "light", 0x24),
    ("light.diffuse_color", "lights", "light", 0x38),
    ("light.diffuse_intensity", "lights", "light", 0x4C),
    ("light.atten_start", "lights", "light", 0x60),
    ("light.atten_end", "lights", "light", 0x74),
    ("light.visibility", "lights", "light", 0x88),
    ("ribbon.color", "ribbons", "ribbon", 0x24),
    ("ribbon.alpha", "ribbons", "ribbon", 0x38),
    ("ribbon.above", "ribbons", "ribbon", 0x4C),
    ("ribbon.below", "ribbons", "ribbon", 0x60),
    ("ribbon.texslot", "ribbons", "ribbon", 0x84),
    ("ribbon.visibility", "ribbons", "ribbon", 0x98),
]
# M2TrackBase (no values array) - the event switch
TRACKBASES = [("event.enabled", "events", "event", 0x18)]


def arr(d, o):
    return struct.unpack_from("<II", d, o)


def u16(d, o):
    return struct.unpack_from("<H", d, o)[0]


def i16(d, o):
    return struct.unpack_from("<h", d, o)[0]


def check(rel, d, skins, bad, notes, tot, honour_level=False):
    """Every unchecked cross-reference of one model and its skins.

    `honour_level`: a submesh's index start is word +8 OR (word +2 << 16) -
    true only for models drawn through a client mod's index-start patch (the races
    pack); every other model is checked exactly as the stock client reads it.
    """
    def fail(rel, kind, msg):
        bad[kind] += 1
        if len(notes) < 60:
            notes.append("%-52s %s" % (os.path.basename(rel), msg))

    if True:
        cnt = {k: arr(d, v)[0] for k, v in H.items()}
        off = {k: arr(d, v)[1] for k, v in H.items()}
        n_seq = cnt["seq"]
        n_bone = cnt["bones"]
        n_glob = cnt["globals"]

        # ---- every array must physically fit in the file -------------------
        for k, sz in (("seq", 64), ("bones", 88), ("colors", 40), ("tex", 16),
                      ("weights", 20), ("transforms", 60), ("attach", 40),
                      ("events", 36), ("lights", 156), ("ribbons", 176),
                      ("verts", 48), ("mat", 4), ("particles", 476)):
            if cnt[k] and off[k] + cnt[k] * sz > len(d):
                fail(rel, "array-overrun", "%s runs past the end of the file" % k)

        # ---- 0x00826235 / 0x0082624c: sequence_lookups ---------------------
        for i in range(cnt["seqlookup"]):
            v = u16(d, off["seqlookup"] + i * 2)
            if v != 0xFFFF and v >= n_seq:
                fail(rel, "seq-lookup",
                     "sequence_lookups[%d] = %d of %d sequences" % (i, v, n_seq))

        # ---- 0x008262b6: variationNext chain, unbounded and unchecked ------
        for i in range(n_seq):
            b = off["seq"] + i * 64
            nxt = u16(d, b + 0x3C)
            alias = u16(d, b + 0x3E)
            if nxt != 0xFFFF and nxt >= n_seq:
                fail(rel, "variation-range",
                     "sequence %d variationNext = %d of %d" % (i, nxt, n_seq))
            if alias != 0xFFFF and alias >= n_seq:
                fail(rel, "alias-range",
                     "sequence %d aliasNext = %d of %d" % (i, alias, n_seq))
            # cycle check - CountVariations has no step limit
            seen, cur, steps = {i}, nxt, 0
            while cur != 0xFFFF and cur < n_seq and steps <= n_seq:
                if cur in seen:
                    fail(rel, "variation-cycle",
                         "sequence %d variation chain loops at %d" % (i, cur))
                    break
                seen.add(cur)
                cur = u16(d, off["seq"] + cur * 64 + 0x3C)
                steps += 1
            tot["sequences"] += 1

        # ---- key bone lookup, replacable lookup, attachment lookup ---------
        for name, limit, what in (("keybone", n_bone, "bones"),
                                  ("attachlookup", cnt["attach"], "attachments"),
                                  ("cameralookup", cnt["cameras"], "cameras")):
            for i in range(cnt[name]):
                v = u16(d, off[name] + i * 2)
                if v != 0xFFFF and v >= limit:
                    fail(rel, name, "%s[%d] = %d of %d %s"
                         % (name, i, v, limit, what))

        # ---- bone parents: range and no cycles -----------------------------
        for i in range(n_bone):
            b = off["bones"] + i * 88
            par = i16(d, b + 8)
            if par != -1 and not (0 <= par < n_bone):
                fail(rel, "bone-parent",
                     "bone %d parent %d of %d" % (i, par, n_bone))
            seen, cur, steps = {i}, par, 0
            while cur != -1 and 0 <= cur < n_bone and steps <= n_bone:
                if cur in seen:
                    fail(rel, "bone-cycle", "bone %d parent chain loops" % i)
                    break
                seen.add(cur)
                cur = i16(d, off["bones"] + cur * 88 + 8)
                steps += 1
        tot["bones"] += n_bone

        # ---- 0x00836ad0 family: a track's global_seq ----------------------
        def check_track(where, base, has_values):
            gs = i16(d, base + 2)
            interp = u16(d, base)
            if interp > 3:
                fail(rel, "interp", "%s interpolation %d" % (where, interp))
            if gs != -1 and not (0 <= gs < n_glob):
                fail(rel, "global-seq",
                     "%s global sequence %d of %d" % (where, gs, n_glob))
            nt, ot = arr(d, base + 4)
            if gs == -1:
                # Per-sequence array of arrays. A track MAY carry fewer lists
                # than the model has sequences - the loader guards every use
                # with "cmp eax, [track.count]; jae skip" (0x00837f70), and
                # the client's own art does it in 0.9% of tracks, retail in
                # 5.7%. It is counted, not failed.
                if nt and nt != n_seq:
                    tot["tracks_short"] += 1
                if nt and ot + nt * 8 > len(d):
                    fail(rel, "track-overrun", "%s timestamps past EOF" % where)
                if has_values:
                    nv, ov = arr(d, base + 0x0C)
                    if nv != nt:
                        fail(rel, "track-pair",
                             "%s %d timestamp lists vs %d value lists"
                             % (where, nt, nv))
                    if nv and ov + nv * 8 > len(d):
                        fail(rel, "track-overrun", "%s values past EOF" % where)
                    for s in range(nt):
                        ct, _ = arr(d, ot + s * 8)
                        cv, _ = arr(d, ov + s * 8)
                        if ct != cv:
                            fail(rel, "track-keys",
                                 "%s sequence %d: %d times, %d values"
                                 % (where, s, ct, cv))
            tot["tracks"] += 1

        for label, akey, skey, toff in TRACKS:
            for i in range(cnt[akey]):
                check_track("%s[%d]" % (label, i),
                            off[akey] + i * SZ[skey] + toff, True)
        for label, akey, skey, toff in TRACKBASES:
            for i in range(cnt[akey]):
                check_track("%s[%d]" % (label, i),
                            off[akey] + i * SZ[skey] + toff, False)

        # ---- things that name a bone --------------------------------------
        def bone_ref(where, v, allow_neg=True):
            if allow_neg and v in (-1, 0xFFFF):
                return
            if not (0 <= v < n_bone):
                fail(rel, "bone-ref", "%s names bone %d of %d"
                     % (where, v, n_bone))

        for i in range(cnt["attach"]):
            bone_ref("attachment %d" % i, u16(d, off["attach"] + i * 40 + 4))
        for i in range(cnt["events"]):
            bone_ref("event %d" % i,
                     struct.unpack_from("<I", d, off["events"] + i * 36 + 8)[0])
        for i in range(cnt["lights"]):
            bone_ref("light %d" % i, i16(d, off["lights"] + i * 156 + 2))
        for i in range(cnt["ribbons"]):
            rb = off["ribbons"] + i * 176
            bone_ref("ribbon %d" % i,
                     struct.unpack_from("<I", d, rb + 4)[0])
            nti, oti = arr(d, rb + 0x14)
            for k in range(nti):
                v = u16(d, oti + k * 2)
                if v >= cnt["tex"]:
                    fail(rel, "ribbon-tex", "ribbon %d texture %d of %d"
                         % (i, v, cnt["tex"]))
            nmi, omi = arr(d, rb + 0x1C)
            for k in range(nmi):
                v = u16(d, omi + k * 2)
                if v >= cnt["mat"]:
                    fail(rel, "ribbon-mat", "ribbon %d material %d of %d"
                         % (i, v, cnt["mat"]))
        for i in range(cnt["particles"]):
            pb = off["particles"] + i * 476
            bone_ref("emitter %d" % i, u16(d, pb + 0x14))
            t = u16(d, pb + 0x16)
            if t >= cnt["tex"]:
                fail(rel, "emitter-tex", "emitter %d texture %d of %d"
                     % (i, t, cnt["tex"]))
        tot["emitters"] += cnt["particles"]

        # ---- vertices name bones through bone_combos ----------------------
        bcombo = [u16(d, off["bonecombo"] + i * 2)
                  for i in range(cnt["bonecombo"])]
        for v in bcombo:
            if v >= n_bone:
                fail(rel, "bonecombo", "bone_combos entry %d of %d bones"
                     % (v, n_bone))

        # ---- the skins ----------------------------------------------------
        for si, sk in enumerate(skins):
            nv, ov = arr(sk, 4 + 8 * 0)
            ni, oi = arr(sk, 4 + 8 * 1)
            nsb, osb = arr(sk, 4 + 8 * 2)
            nsm, osm = arr(sk, 4 + 8 * 3)
            nba, oba = arr(sk, 4 + 8 * 4)
            for k in range(nv):
                v = u16(sk, ov + k * 2)
                if v >= cnt["verts"]:
                    fail(rel, "skin-vert", "skin %d vertex %d of %d model verts"
                         % (si, v, cnt["verts"]))
                    break
            for k in range(ni):
                v = u16(sk, oi + k * 2)
                if v >= nv:
                    fail(rel, "skin-index",
                         "skin %d index %d of %d skin verts" % (si, v, nv))
                    break
            for k in range(nsm):
                sb = osm + k * 48
                vs, vc = u16(sk, sb + 4), u16(sk, sb + 6)
                ist, ic = u16(sk, sb + 8), u16(sk, sb + 0x0A)
                if honour_level:
                    ist |= u16(sk, sb + 2) << 16
                bc, bci = u16(sk, sb + 0x0C), u16(sk, sb + 0x0E)
                cb = u16(sk, sb + 0x12)
                if vs + vc > nv:
                    fail(rel, "submesh-verts",
                         "skin %d submesh %d spans %d..%d of %d"
                         % (si, k, vs, vs + vc, nv))
                if ist + ic > ni:
                    fail(rel, "submesh-indices",
                         "skin %d submesh %d spans %d..%d of %d"
                         % (si, k, ist, ist + ic, ni))
                if bci + bc > nsb:
                    fail(rel, "submesh-bones",
                         "skin %d submesh %d reads bones %d..%d of %d"
                         % (si, k, bci, bci + bc, nsb))
                # 0x00822120: bone_matrices[submesh.centerBoneIndex]
                if cb >= n_bone:
                    fail(rel, "submesh-centerbone",
                         "skin %d submesh %d centerBoneIndex %d of %d bones"
                         % (si, k, cb, n_bone))
            for k in range(nba):
                bb = oba + k * 24
                sub = u16(sk, bb + 0x04)
                col = u16(sk, bb + 0x08)
                mat = u16(sk, bb + 0x0A)
                tc = u16(sk, bb + 0x0E)
                shid = u16(sk, bb + 0x02)
                tcb = u16(sk, bb + 0x10)
                ccb = u16(sk, bb + 0x12)
                wcb = u16(sk, bb + 0x14)
                xcb = u16(sk, bb + 0x16)
                w = "skin %d batch %d" % (si, k)
                if sub >= nsm:
                    fail(rel, "batch-submesh", "%s draws submesh %d of %d"
                         % (w, sub, nsm))
                if col != 0xFFFF and col >= cnt["colors"] and cnt["colors"]:
                    fail(rel, "batch-color", "%s colorIndex %d of %d"
                         % (w, col, cnt["colors"]))
                if mat >= cnt["mat"]:
                    fail(rel, "batch-material", "%s material %d of %d"
                         % (w, mat, cnt["mat"]))
                if shid & 0x8000 and (shid & 0x7FFF) > 3:
                    fail(rel, "SHADER-CRASH",
                         "%s ships table shader 0x%04X; 0x00836ccf jumps to "
                         "the strcpy at 0x00836d07 with a NULL prefix for any "
                         "index above 3 and the client dies at 0x00836d11"
                         % (w, shid))
                if shid == 0x8000:
                    fail(rel, "batch-dropped",
                         "%s ships shader 0x8000, which 0x00821e97 removes "
                         "from the draw entirely" % w)
                # 008369BE tests header flag 0x8. Without it, 008369D1..6A11
                # synthesizes the shader from material/UV fields and never
                # reads the optional combiner array at header+0x130.
                if (cnt['gflags'] & 0x8) and not (shid & 0x8000) and shid + tc > cnt["combiner"]:
                    fail(rel, "batch-combiner", "%s combiner %d..%d of %d"
                         % (w, shid, shid + tc - 1, cnt["combiner"]))
                if ccb + tc > cnt["coord"]:
                    fail(rel, "batch-coord", "%s coord %d..%d of %d"
                         % (w, ccb, ccb + tc - 1, cnt["coord"]))
                if tcb + tc > cnt["texcombo"]:
                    fail(rel, "batch-texcombo", "%s texcombo %d..%d of %d"
                         % (w, tcb, tcb + tc - 1, cnt["texcombo"]))
                # The client reads ONE weight combo per batch, not one per
                # unit: 0x00821ed2 / 0x00827b97 / 0x00827cb5 / 0x00828090 all
                # do movzx eax, word [batch+0x14] then index the array once.
                if cnt["weightcombo"] and wcb >= cnt["weightcombo"]:
                    fail(rel, "batch-weightcombo", "%s weightcombo %d of %d"
                         % (w, wcb, cnt["weightcombo"]))
                if xcb + tc > cnt["transcombo"]:
                    fail(rel, "batch-transcombo", "%s transcombo %d..%d of %d"
                         % (w, xcb, xcb + tc - 1, cnt["transcombo"]))
                for u in range(tc):
                    if wcb + u < cnt["weightcombo"]:
                        v = u16(d, off["weightcombo"] + (wcb + u) * 2)
                        if v != 0xFFFF and v >= cnt["weights"]:
                            fail(rel, "weight-ref",
                                 "%s weight %d of %d" % (w, v, cnt["weights"]))
                    if tcb + u < cnt["texcombo"]:
                        v = u16(d, off["texcombo"] + (tcb + u) * 2)
                        if v >= cnt["tex"]:
                            fail(rel, "tex-ref",
                                 "%s texture %d of %d" % (w, v, cnt["tex"]))
                tot["batches"] += 1


def report(n, bad, notes, tot):
    print("models checked : %d" % n)
    print("short tracks (legal, loader guards them): %d" % tot["tracks_short"])
    print("sequences %d  bones %d  tracks %d  batches %d  emitters %d"
          % (tot["sequences"], tot["bones"], tot["tracks"], tot["batches"],
             tot["emitters"]))
    if not bad:
        print("\nevery unchecked cross-reference is in range")
        return 0
    print("")
    for k, v in bad.most_common():
        print("  %-22s %d" % (k, v))
    print("")
    for s in notes:
        print("  " + s)
    return 1


if __name__ == "__main__":
    raise SystemExit("library module: run `python bp.py audit` instead")
