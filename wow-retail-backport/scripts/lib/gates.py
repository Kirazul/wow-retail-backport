r"""
gates.py - `bp.py audit`: every check that stands between a build and the
player's screen. Exit 1 when any FAIL exists. Each gate names the in-game
symptom it prevents (references/troubleshooting.md has the full map).

CLOSURE  (checker box, "missing" item)
  C1  every file a row names exists: helm per client race+sex, shoulder L/R,
      weapon/shield, row textures, the 8 regions (_U, or _M and _F), the icon
  C2  every model's 00.skin, its type-0 texture files, the .anim of every
      external sequence (id <= 505)
MODEL    (crash, black, white, flying, turning with the camera, vanishing)
  M1  check_264: flat MD20 v264, every array inside the file, every index
      the client reads unchecked is in range (m2_port)
  M2  verify_bounds: the reads wow.exe does WITHOUT a bounds check (crash sites)
  M3  bone palette: every weighted vertex skins with its own bone (a vertex on
      a billboard bone turns with the camera / floats)
  M4  a one-bone model whose bone is a billboard (the whole item turns with the camera)
  M5  sequence 0 lives in the model (the client reads it without an .anim: crash)
  M6  the bounding boxes contain the mesh (the client culls by the box: vanishes on zoom)
  M7  white: a batch samples a texture slot nothing binds (type 0 with no file,
      type 2 on a row with no ModelTexture, any other type on an item)
  M8  motion: Stand + every global loop sampled 16 times: no non-finite matrix,
      no vertex travelling > 3 m, no collapse below 10 % (parts flying/spinning off)
TEXTURE  (crash, green, garbage)
  T1  BLP2, power-of-two sides <= max_texture, encoding 1 or 2 (never 3),
      every mip inside the file
  T2  a body-region (TextureComponents) texture is PALETTISED (encoding 1):
      a DXT one draws missing-texture GREEN on the 3.3.5 compositor
PLACEMENT (floating / sunk helms)
  P1  each built helm on each client body, at that body's helm point: the helm
      must reach the head (nearest helm vertex within the retail-on-retail
      distance + 8 cm, centroid within 12 cm of retail's, head-relative)
TABLES
  D1  out/dbc tables parse; every new display row's strings resolve; ids unique
"""
from __future__ import annotations

import collections
import os
import struct

import numpy as np

import blp
import bodies as BD
import bpcore
import m2_port as MP
import m2edit as E
import m2model as M
import m2_view as MV
import retail_m2 as RM
import scan as SC
import verify_bounds as VB
from wdbc import Wdbc

REGION_DIR = ("ArmUpperTexture", "ArmLowerTexture", "HandTexture", "TorsoUpperTexture",
              "TorsoLowerTexture", "LegUpperTexture", "LegLowerTexture", "FootTexture")
FOLDER = {"head": "Head", "shoulder": "Shoulder", "back": "Cape"}


class Report:
    def __init__(self):
        self.fails = collections.defaultdict(list)
        self.notes = collections.defaultdict(list)
        self.counts = collections.Counter()

    def fail(self, gate, what):
        self.fails[gate].append(what)

    def note(self, gate, what):
        self.notes[gate].append(what)


def closure(prj, plan, sc):
    """{archive path (lower): (path, why)} every file the rows reach."""
    root = prj.build_dir
    man = bpcore.load_json(os.path.join(root, "manifest.json"), {})
    want = {}

    def add(path, why):
        want.setdefault(path.lower(), (path, why))
    for d, p in plan.items():
        if not p.get("ok"):
            continue
        r = p["row"]
        slot = p["slot"]
        folder = FOLDER.get(slot) or ("Shield" if p.get("inv") == 14 else "Weapon")
        for k in (0, 1):
            mname = r["model"][k]
            if mname:
                stem = mname[:-4]
                if slot == "head":
                    for race in sc["races"]:
                        for sx in race["bodies"]:
                            add("Item\\ObjectComponents\\Head\\%s_%s%s.m2" % (stem, race["prefix"], sx), "d%s helm" % d)
                else:
                    add("Item\\ObjectComponents\\%s\\%s.m2" % (folder, stem), "d%s model %d" % (d, k))
            if r["tex"][k]:
                add("Item\\ObjectComponents\\%s\\%s.blp" % (folder, r["tex"][k]), "d%s texture %d" % (d, k))
        for i, name in enumerate(r["regions"]):
            if name:
                base = "Item\\TextureComponents\\%s\\%s" % (REGION_DIR[i], name)
                if (base + "_U.blp").lower() in man:
                    add(base + "_U.blp", "d%s region %d" % (d, i))
                else:
                    add(base + "_M.blp", "d%s region %d" % (d, i))
                    add(base + "_F.blp", "d%s region %d" % (d, i))
        if r["icon"]:
            add("Interface\\Icons\\%s.blp" % r["icon"], "d%s icon" % d)
    # what each model needs
    for low, (path, why) in list(want.items()):
        if not low.endswith(".m2"):
            continue
        add(path[:-3] + "00.skin", why + " skin")
        f = os.path.join(root, path)
        if not os.path.isfile(f):
            continue
        body = open(f, "rb").read()
        n, o = struct.unpack_from("<II", body, MP.H_TEXTURES)
        for i in range(n):
            typ, _fl, ln, no = struct.unpack_from("<IIII", body, o + i * 16)
            if typ == 0 and ln > 1:
                add(body[no:no + ln].split(b"\0")[0].decode("latin1"), why + " texture")
        for _s, a, v in M.external_set(body):
            if a <= 505:
                add(M.anim_name(path, a, v), why + " anim")
    return want, man


def model_checks(rep: Report, rel: str, body: bytes, skin: bytes, has_row_tex: bool, anims: dict):
    try:
        MP.check_264(body, [skin])
    except Exception as ex:   # noqa: BLE001
        rep.fail("M1 check_264", "%s: %s" % (rel, ex))
        return
    bad, notes, tot = collections.Counter(), [], collections.Counter()
    try:
        VB.check(rel, body, [skin], bad, notes, tot)
    except Exception as ex:   # noqa: BLE001
        bad["exception: %s" % ex] += 1
    for k, n in bad.items():
        rep.fail("M2 unchecked reads", "%s: %s x%d" % (rel, k, n))
    if E.palette_mismatches(body, skin):
        rep.fail("M3 bone palette", "%s: %d influences on another bone" % (rel, E.palette_mismatches(body, skin)))
    if E.single_bone_billboard(body):
        rep.fail("M4 billboard", rel)
    ns, so = struct.unpack_from("<II", body, 0x1C)
    if ns:
        fl = struct.unpack_from("<I", body, so + 0x0C)[0]
        if not fl & 0x20 and not fl & 0x40:
            rep.fail("M5 sequence 0 external", rel)
    P = E.positions(body)
    if len(P):
        lo = np.array(struct.unpack_from("<3f", body, 0xA0))
        hi = np.array(struct.unpack_from("<3f", body, 0xAC))
        if (P.min(0) < lo - 0.01).any() or (P.max(0) > hi + 0.01).any():
            rep.fail("M6 bounds", "%s: mesh %s..%s outside box %s..%s" % (rel, P.min(0).round(2), P.max(0).round(2),
                                                                          lo.round(2), hi.round(2)))
    # M7 white
    n, o = struct.unpack_from("<II", body, MP.H_TEXTURES)
    kinds = []
    for i in range(n):
        typ, _fl, ln, _no = struct.unpack_from("<IIII", body, o + i * 16)
        kinds.append((typ, ln > 1))
    nl, ol = struct.unpack_from("<II", body, 0x80)
    look = struct.unpack_from("<%dh" % nl, body, ol) if nl else ()
    sk = RM.skin(skin)
    for bt in sk["batches"]:
        for u in range(bt[8]):
            if bt[9] + u >= len(look):
                continue
            ti = look[bt[9] + u]
            if not 0 <= ti < len(kinds):
                continue
            typ, named = kinds[ti]
            if (typ == 0 and not named) or (typ == 2 and not has_row_tex) or typ not in (0, 2):
                rep.fail("M7 draws white", "%s: batch on texture slot %d (type %d)" % (rel, ti, typ))
                break
    motion(rep, rel, body, sk, anims)


def motion(rep: Report, rel: str, body: bytes, sk: dict, anims: dict):
    try:
        b = bytearray(body)
        struct.pack_into("<IIII", b, 0x120, 0, 0, 0, 0)          # effects are not geometry
        ext = {s: anims.get((a, v)) for s, a, v in M.external_set(body)}
        m = M.read(bytes(b), ext=lambda s: ext.get(s))
    except Exception as ex:   # noqa: BLE001 - a model the sampler cannot read is not trusted
        rep.fail("M8 motion", "%s: cannot be read for animation (%s)" % (rel, ex))
        return
    if not m.a["bones"]:
        return
    geos = {s["id"] for s in sk["subs"]}
    bind = MV.mesh(m, sk, geos, np.array([np.eye(4)] * len(m.a["bones"])))[0]
    size = float(np.ptp(bind, axis=0).max()) if len(bind) else 0
    runs = [(0, None)] + [(-1, g) for g in range(len(m.a.get("gloops", [])))]
    for seq, g in runs:
        dur = m.seq_field(0, "dur") if g is None and m.a["seqs"] else (m.a["gloops"][g] if g is not None else 0)
        for t in np.linspace(0, max(1, dur), 16):
            mats = MV.bone_matrices(m, 0 if seq < 0 else seq, float(t))
            if not np.isfinite(mats).all():
                rep.fail("M8 motion", "%s: non-finite bone matrix at t=%d" % (rel, t))
                return
            pos = MV.mesh(m, sk, geos, mats)[0]
            if len(pos) and np.linalg.norm(pos - bind, axis=1).max() > 3.0:
                rep.fail("M8 motion", "%s: a vertex travels %.1f m (t=%d)" % (rel, np.linalg.norm(pos - bind, axis=1).max(), t))
                return
            if size > 0.05 and len(pos) and float(np.ptp(pos, axis=0).max()) < 0.1 * size:
                rep.fail("M8 motion", "%s: the mesh collapses (t=%d)" % (rel, t))
                return


def texture_checks(rep: Report, rel: str, d: bytes, max_edge: int):
    if d[:4] != b"BLP2":
        rep.fail("T1 texture", "%s: not BLP2" % rel)
        return
    typ, enc, _ad, _at, _mips, w, h = struct.unpack_from("<IBBBBII", d, 4)
    offs = struct.unpack_from("<16I", d, 20)
    sizes = struct.unpack_from("<16I", d, 84)
    if w > max_edge or h > max_edge or w & (w - 1) or h & (h - 1):
        rep.fail("T1 texture", "%s: %dx%d" % (rel, w, h))
    if enc not in (1, 2):
        rep.fail("T1 texture", "%s: encoding %d" % (rel, enc))
    for i in range(16):
        if offs[i] and offs[i] + sizes[i] > len(d):
            rep.fail("T1 texture", "%s: mip %d outside the file" % (rel, i))
            break
        if offs[i] and sizes[i] and offs[i] < 1172:
            rep.fail("T1 texture", "%s: mip %d starts inside the 1172-byte BLP2 header" % (rel, i))
            break
    if "\\texturecomponents\\" in rel.lower() and enc != 1:
        rep.fail("T2 green", "%s: body-region texture is not palettised (draws green)" % rel)


def placement(prj, rep: Report, plan, sc):
    """P1: every built helm reaches the head of every client body."""
    root = prj.build_dir
    man = bpcore.load_json(os.path.join(root, "manifest.json"), {})
    retail_ref = {}
    bodies = {}
    for d, p in plan.items():
        if not p.get("ok") or p["slot"] != "head" or not p["row"]["model"][0]:
            continue
        stem = p["row"]["model"][0][:-4]
        for race in sc["races"]:
            for sx in race["bodies"]:
                rel = "Item\\ObjectComponents\\Head\\%s_%s%s.m2" % (stem, race["prefix"], sx)
                e = man.get(rel.lower())
                if not e or e["kind"] != "helm":
                    continue
                key = (race["id"], sx)
                if key not in bodies:
                    bodies[key] = BD.client_body(prj, race, sx)
                cb = bodies[key]
                if cb is None or cb.helm_point() is None or cb.head_pivot() is None:
                    rep.note("P1 placement", "%s: client body has no helm point/head bone" % rel)
                    continue
                hv = E.positions(open(os.path.join(root, rel), "rb").read())
                if not len(hv):
                    continue
                world = hv + cb.helm_point()
                near = float(np.linalg.norm(world - cb.head_pivot(), axis=1).min())
                cen = world.mean(0) - cb.head_pivot()
                src = e["src"]
                rk = (src.get("from"), src.get("src_race"), src.get("src_sex"))
                if rk not in retail_ref:
                    retail_ref[rk] = None
                    try:
                        rb = BD.retail_body(prj, rk[1], rk[2]) if rk[1] is not None else None
                        raw = BD.Files(prj).read(rk[0])
                        rv = np.array([v[0] for v in RM.vertices(RM.md20(raw))], dtype=np.float64)
                        if rb is not None and rb.helm_point() is not None and rb.head_pivot() is not None and len(rv):
                            if float(np.median(rv[:, 2])) > 0.9:       # body-space helm: already in body space
                                rw = rv
                            else:
                                rw = rv + rb.helm_point()
                            retail_ref[rk] = (float(np.linalg.norm(rw - rb.head_pivot(), axis=1).min()),
                                              rw.mean(0) - rb.head_pivot())
                    except Exception:   # noqa: BLE001
                        retail_ref[rk] = None
                ref = retail_ref[rk]
                rep.counts["P1 helms checked"] += 1
                if ref is None:
                    if near > 0.30:
                        rep.fail("P1 placement", "%s: nearest vertex %.2f m from the head (no retail reference)" % (rel, near))
                    continue
                if near > ref[0] + 0.08 or float(np.linalg.norm(cen - ref[1])) > 0.12:
                    rep.fail("P1 placement", "%s: head distance %.2f m (retail %.2f), centroid %.2f m off retail's"
                             % (rel, near, ref[0], float(np.linalg.norm(cen - ref[1]))))


def run(prj, log) -> int:
    plan = bpcore.load_json(prj.path("build", "plan.json"))
    sc = SC.load(prj)
    rep = Report()
    root = prj.build_dir
    want, man = closure(prj, plan, sc)
    rep.counts["files in closure"] = len(want)
    max_edge = int(prj.cfg.get("max_texture") or 1024)
    row_tex = set()
    for p in plan.values():
        if p.get("ok"):
            for k in (0, 1):
                if p["row"]["model"][k] and p["row"]["tex"][k]:
                    row_tex.add(p["row"]["model"][k][:-4].rsplit("\\", 1)[-1].lower())
    for low, (path, why) in sorted(want.items()):
        f = os.path.join(root, path)
        if not os.path.isfile(f):
            rep.fail("C1/C2 closure", "%s missing (%s)" % (path, why))
            continue
        if low.endswith(".blp"):
            texture_checks(rep, path, open(f, "rb").read(), max_edge)
            rep.counts["textures"] += 1
        elif low.endswith(".m2"):
            body = open(f, "rb").read()
            sk_path = f[:-3] + "00.skin"
            if not os.path.isfile(sk_path):
                continue
            anims = {}
            for _s, a, v in M.external_set(body):
                ap = os.path.join(root, M.anim_name(path, a, v))
                if os.path.isfile(ap):
                    anims[(a, v)] = open(ap, "rb").read()
            stem = path.rsplit("\\", 1)[-1][:-3].lower()
            has_row = any(stem.startswith(s) for s in row_tex)
            invisible = man.get(low, {}).get("kind") == "helm-invisible"
            if not invisible:
                model_checks(rep, path, body, open(sk_path, "rb").read(), has_row, anims)
            rep.counts["models"] += 1
    placement(prj, rep, plan, sc)
    # D1 tables
    for n in ("ItemDisplayInfo", "Item", "HelmetGeosetVisData"):
        p = prj.path("out", "dbc", n + ".dbc")
        if not os.path.isfile(p):
            rep.fail("D1 tables", "%s.dbc not written: run tables" % n)
            continue
        t = Wdbc(open(p, "rb").read())
        if len(t.ids()) != len(t.rows):
            rep.fail("D1 tables", "%s.dbc has duplicate ids" % n)
    for d, p in plan.items():
        if not p.get("ok"):
            rep.note("not shipped", "display %s: %s" % (d, p.get("error")))
        elif p.get("report", {}).get("tier2_only"):
            rep.note("stock-client limits", "display %s (%s): %s" % (d, p["slot"], p["report"]["tier2_only"]))
    shifts = bpcore.load_json(prj.path("client", "helm_shift.json"), {})
    for k, v in sorted(shifts.items()):
        if v.get("how") != "measured":
            rep.note("helm shift", "%s: %s - look at its sheet" % (k, v.get("how")))
    out = {"fails": rep.fails, "notes": rep.notes, "counts": dict(rep.counts)}
    bpcore.save_json(prj.path("reports", "audit.json"), out)
    log("audit: %s" % ", ".join("%s %d" % kv for kv in rep.counts.items()))
    for g, items in sorted(rep.notes.items()):
        log("  note  %-22s %d  e.g. %s" % (g, len(items), items[0]))
    if not rep.fails:
        log("audit: PASS (0 problems)")
        log("next: python bp.py pack --work <W>")
        return 0
    for g, items in sorted(rep.fails.items()):
        log("  FAIL  %-22s %d" % (g, len(items)))
        for x in items[:8]:
            log("        %s" % x)
    log("audit: FAIL - fix the rule in the converter (never the output file), rebuild, audit again")
    return 1
