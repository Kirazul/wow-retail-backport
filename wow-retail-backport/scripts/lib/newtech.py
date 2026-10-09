r"""
newtech.py - `bp.py newtech`: catch anything retail does that this kit has
never seen, BEFORE it is converted wrong.

  --learn   record what retail files use (from the cache) into
            <work>/reports/known_tech.json, merged with the kit's own
            registry (scripts/data/known_tech.json, measured on retail
            12.1.0.69933 over every cached model, skin, texture and table)
  (check)   scan every file the selection uses; anything NOT in the registry
            -> exit 1 and a list of what is new

WHY: every rule in this kit was measured on retail data at one build. A new
expansion can add a chunk (a new file type a model names), a flag bit, a
blend mode, a texture type, a BLP format or a table column. Converted by the
old rules, that is the next "white sheet" or crash. So the kit stops, and the
agent researches the new thing (wowdev.wiki, wow.export / WMV source, the
retail file itself), writes the rule WITH a test, and only then continues
(SKILL.md "When newtech stops you").

What is recorded per kind:
  m2:   top-level chunk magics, MD21 version, global flag bits, bone flag bits,
        material flag bits, material blend modes, texture types, texture flag
        bits, particle flag bits, particle blend types, sequence flag bits
  skin: magic, header size class
  blp:  (type, encoding, alpha depth, alpha type) combinations
  anim / skel / phys: top-level chunk magics
  db2:  the column list of every table the kit reads
"""
from __future__ import annotations

import collections
import json
import os
import struct

import bpcore
import m2_port as MP
import retail_m2 as RM
from wago import Db2, Files

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KIT_REGISTRY = os.path.join(HERE, "data", "known_tech.json")
TABLES = ("TransmogSet", "TransmogSetItem", "ItemModifiedAppearance", "ItemAppearance", "Item", "ItemSparse",
          "ItemDisplayInfo", "ItemDisplayInfoModelMatRes", "ItemDisplayInfoMaterialRes", "ModelFileData",
          "TextureFileData", "ComponentModelFileData", "ComponentTextureFileData", "ChrRaces",
          "HelmetGeosetData", "ParticleColor", "ItemSubClass", "ChrRaceXChrModel", "ChrModel",
          "CreatureDisplayInfo", "CreatureModelData", "ItemNameDescription")


def bits(v: int) -> list[int]:
    return [1 << i for i in range(32) if v >> i & 1]


def features(data: bytes) -> dict:
    """{feature name: set of values} found in one retail file."""
    f = collections.defaultdict(set)
    head = data[:4]
    if head == b"BLP2":
        typ, enc, ad, at = struct.unpack_from("<IBBB", data, 4)
        f["blp.format"].add("%d/%d/%d/%d" % (typ, enc, ad, at))
        return f
    if head == b"SKIN":
        f["skin.magic"].add("SKIN")
        return f
    if head in (b"MD21", b"MD20"):
        c = RM.chunks(data)
        for k in c:
            f["m2.chunk"].add(k)
        body = MP.unwrap(data)[0]
        f["m2.version"].add(struct.unpack_from("<I", body, 4)[0])
        f["m2.global_flags"].update(bits(struct.unpack_from("<I", body, 0x10)[0]))
        n, o = struct.unpack_from("<II", body, 0x2C)
        for i in range(n):
            f["m2.bone_flags"].update(bits(struct.unpack_from("<I", body, o + i * 88 + 4)[0]))
        n, o = struct.unpack_from("<II", body, 0x70)
        for i in range(n):
            fl, bl = struct.unpack_from("<HH", body, o + i * 4)
            f["m2.material_flags"].update(bits(fl))
            f["m2.material_blend"].add(bl)
        n, o = struct.unpack_from("<II", body, 0x50)
        for i in range(n):
            typ, fl = struct.unpack_from("<II", body, o + i * 16)
            f["m2.texture_type"].add(typ)
            f["m2.texture_flags"].update(bits(fl))
        n, o = struct.unpack_from("<II", body, 0x1C)
        for i in range(n):
            f["m2.sequence_flags"].update(bits(struct.unpack_from("<I", body, o + i * 64 + 0x0C)[0]))
        n, o = struct.unpack_from("<II", body, 0x128)
        for i in range(n):
            rec = o + i * MP.SZ_PARTICLE_RETAIL
            if rec + 0x2C > len(body):
                break
            f["m2.particle_flags"].update(bits(struct.unpack_from("<I", body, rec + 4)[0]))
            f["m2.particle_blend"].add(body[rec + 0x28])
        return f
    if head in (b"AFM2", b"AFSA", b"AFSB"):
        for k in RM.chunks(data):
            f["anim.chunk"].add(k)
        return f
    if head == b"SKL1":
        for k in RM.chunks(data):
            f["skel.chunk"].add(k)
        return f
    if head in (b"SYHP", b"PHYS"):
        try:
            for k in RM.chunks(data):
                f["phys.chunk"].add(k)
        except ValueError:
            f["phys.chunk"].add(head.decode("latin1"))
        return f
    return f


def _load(path):
    if not os.path.isfile(path):
        return {}
    return {k: set(v) for k, v in json.load(open(path, encoding="utf-8")).items()}


def registry(prj) -> dict:
    reg = _load(KIT_REGISTRY)
    for k, v in _load(prj.path("reports", "known_tech.json")).items():
        reg.setdefault(k, set()).update(v)
    return reg


def _norm(v):
    return v if isinstance(v, str) else int(v)


def run(prj, log, learn: bool) -> int:
    F = Files(prj)
    db = Db2(prj)
    if learn:
        reg = collections.defaultdict(set)
        names = [n for n in os.listdir(F.dir) if n.isdigit()]
        log("learning from %d cached retail files..." % len(names))
        for i, n in enumerate(names, 1):
            try:
                d = open(os.path.join(F.dir, n), "rb").read()
                if not d.strip(b"\0"):
                    continue
                for k, v in features(d).items():
                    reg[k].update(_norm(x) for x in v)
            except Exception:   # noqa: BLE001 - a file we cannot parse teaches nothing
                pass
            if i % 5000 == 0:
                log("  %d/%d" % (i, len(names)))
        for t in TABLES:
            try:
                reg["db2.%s" % t].update(db.header(t))
            except Exception as ex:   # noqa: BLE001
                log("  table %s: %s" % (t, ex))
        out = {k: sorted(v, key=str) for k, v in reg.items()}
        bpcore.save_json(prj.path("reports", "known_tech.json"), out)
        log("recorded %d feature kinds -> reports/known_tech.json" % len(out))
        return 0
    reg = registry(prj)
    if not reg:
        log("FAIL: no registry: run  python bp.py newtech --learn  once for this build")
        return 1
    sel = bpcore.load_json(prj.path("selection.json"))
    ids = set()
    for e in sel["displays"].values():
        for key in ("models", "model_tex", "extra_tex", "sections"):
            ids.update(x["fdid"] for x in e.get(key, []))
    # what the models name, recursively (the fetch step downloaded them)
    todo, seen = list(ids), set()
    while todo:
        f = todo.pop()
        if f in seen:
            continue
        seen.add(f)
        d = F.try_read(f)
        if d and d[:4] in (b"MD21", b"MD20", b"SKL1"):
            for kind, xs in RM.deps(d).items():
                for x in (xs[:1] if kind == "skin" else xs):
                    todo.append(int(x))
    new = collections.defaultdict(set)
    where = collections.defaultdict(list)
    for f in sorted(seen):
        d = F.try_read(f)
        if not d:
            continue
        try:
            got = features(d)
        except Exception as ex:   # noqa: BLE001
            new["unparsable file"].add(str(ex)[:80])
            where["unparsable file"].append(f)
            continue
        for k, vals in got.items():
            for v in vals:
                if _norm(v) not in reg.get(k, set()) and str(v) not in {str(x) for x in reg.get(k, set())}:
                    new[k].add(v)
                    if len(where[k]) < 5:
                        where[k].append(f)
    for t in TABLES:
        try:
            cols = set(db.header(t))
        except Exception:   # noqa: BLE001
            continue
        known = reg.get("db2.%s" % t, set())
        if known and cols - known:
            new["db2.%s" % t].update(cols - known)
    rep = {k: sorted(map(str, v)) for k, v in new.items()}
    bpcore.save_json(prj.path("reports", "newtech.json"), {"new": rep, "examples": where})
    log("newtech: %d files checked against %d known feature kinds" % (len(seen), len(reg)))
    if not new:
        log("newtech: PASS - nothing retail uses here is new to this kit")
        log("next: python bp.py measure --work <W>")
        return 0
    for k, v in sorted(rep.items()):
        log("  NEW  %-22s %s   e.g. files %s" % (k, v[:12], where.get(k, [])[:3]))
    log("newtech: STOP - research each NEW item and add its rule + test before building (SKILL.md)")
    return 1
