r"""
provegates.py - `bp.py prove-gates`: show that the audit catches real defects
on YOUR build, not just that it passes. A passing gate proves nothing until
it has been seen to fail.

Copies <work> (without the retail cache and tables) to <work>_prove, plants
one defect of each kind into files the rows actually ship, runs the audit on
the copy and requires every defect to be reported:

  floating helm      a helm raised 35 cm                 -> P1 placement
  green texture      a body-region texture made DXT      -> T2 green
  missing file       an icon deleted                     -> C1/C2 closure
  wrong bone         a submesh's bone palette moved      -> M3 bone palette
  culled item        a model's bounding box shrunk       -> M6 bounds
The copy is deleted afterwards. Exit 0 only when all five are caught.
"""
from __future__ import annotations

import json
import os
import shutil
import struct
import subprocess
import sys

import blp
import m2edit as E


def run(prj, log) -> int:
    W = prj.work.rstrip("\\/") + "_prove"
    if os.path.exists(W):
        shutil.rmtree(W)
    shutil.copytree(prj.work, W, ignore=shutil.ignore_patterns("cache", "db2", "listfile.csv", "sheets", "logs"))
    cfg = json.load(open(os.path.join(W, "bp.json"), encoding="utf-8"))
    cfg["cache"] = prj.cache_dir
    if not cfg.get("listfile"):
        cfg["listfile"] = os.path.join(prj.work, "listfile.csv")
    json.dump(cfg, open(os.path.join(W, "bp.json"), "w", encoding="utf-8"), indent=1)
    shutil.copytree(prj.db2_dir, os.path.join(W, "db2", prj.build))
    B = os.path.join(W, "build")
    plan = json.load(open(os.path.join(B, "plan.json"), encoding="utf-8"))
    man = json.load(open(os.path.join(B, "manifest.json"), encoding="utf-8"))
    planted = []
    helm = next((v["path"] for v in man.values() if v["kind"] == "helm"), None)
    if helm:
        p = os.path.join(B, helm)
        data = open(p, "rb").read()
        open(p, "wb").write(E.union_bounds(E.translate(data, (0, 0, 0.35)))[0])
        planted.append("P1 placement")
    sec = next((v["path"] for v in man.values() if v["kind"] == "section"), None)
    if sec:
        p = os.path.join(B, sec)
        img = blp.decode(open(p, "rb").read())
        open(p, "wb").write(blp.encode(img, "dxt1"))
        planted.append("T2 green")
    icon = next((v["path"] for v in man.values() if v["kind"] == "icon"), None)
    if icon:
        os.remove(os.path.join(B, icon))
        planted.append("C1/C2 closure")
    # a wrong bone palette: planted only where it really moves a vertex to
    # another bone (a model whose bone lookup is all one bone cannot show it)
    model = None
    for v in plan.values():
        if not (v.get("ok") and v["slot"] in ("shoulder", "mainhand", "offhand", "ranged", "held")):
            continue
        folder = "Shoulder" if v["slot"] == "shoulder" else ("Shield" if v.get("inv") == 14 else "Weapon")
        for k in (0, 1):
            if not v["row"]["model"][k]:
                continue
            rel = "Item\\ObjectComponents\\%s\\%s.m2" % (folder, v["row"]["model"][k][:-4])
            body = open(os.path.join(B, rel), "rb").read()
            s = bytearray(open(os.path.join(B, rel[:-3] + "00.skin"), "rb").read())
            n, o = struct.unpack_from("<II", s, 0x1C)
            for i in range(n):
                at = o + i * 48 + 0x0E
                old = struct.unpack_from("<H", s, at)[0]
                struct.pack_into("<H", s, at, old + 1)
                if E.palette_mismatches(body, bytes(s)):
                    open(os.path.join(B, rel[:-3] + "00.skin"), "wb").write(bytes(s))
                    model = rel
                    break
                struct.pack_into("<H", s, at, old)
            if model:
                break
        if model:
            break
    if model:
        planted.append("M3 bone palette")
    else:
        log("  (no shipped model has more than one bone in a palette: M3 cannot be planted here)")
    if model or (model := next((v["path"] for v in man.values() if v["kind"] in ("weapon", "shoulder", "shield")
                                and os.path.isfile(os.path.join(B, v["path"]))), None)):
        p = os.path.join(B, model)
        m = bytearray(open(p, "rb").read())
        struct.pack_into("<7f", m, 0xA0, 0, 0, 0, 0.01, 0.01, 0.01, 0.01)
        open(p, "wb").write(bytes(m))
        planted.append("M6 bounds")
    # white: a texture slot a batch samples turned into a slot nothing binds;
    # flying: a keyed bone moved 10 m; crash: sequence 0 marked external
    others = [v["path"] for v in man.values() if v["kind"] in ("helm",) and os.path.isfile(os.path.join(B, v["path"]))]
    target = next((x for x in others if x != helm), None)
    if target:
        p = os.path.join(B, target)
        m = bytearray(open(p, "rb").read())
        nl, ol = struct.unpack_from("<II", m, 0x80)
        n, o = struct.unpack_from("<II", m, 0x50)
        if nl and n:
            ti = struct.unpack_from("<h", m, ol)[0]
            struct.pack_into("<I", m, o + ti * 16, 3)
            planted.append("M7 draws white")
        ns, so = struct.unpack_from("<II", m, 0x1C)
        if ns:
            fl = struct.unpack_from("<I", m, so + 0x0C)[0]
            struct.pack_into("<I", m, so + 0x0C, fl & ~0x60)
            planted.append("M5 sequence 0 external")
        open(p, "wb").write(bytes(m))
    # flying: a bone keyed in the model (Stand or a global loop) moved 10 m
    shipped = [v["path"] for v in man.values() if v["kind"] in ("helm", "shoulder", "weapon", "shield")
               and v["path"] not in (helm, target, model) and os.path.isfile(os.path.join(B, v["path"]))]
    for rel in shipped:
        p = os.path.join(B, rel)
        m = bytearray(open(p, "rb").read())
        moved = False
        nb, ob = struct.unpack_from("<II", m, 0x2C)
        ns, so = struct.unpack_from("<II", m, 0x1C)
        resident0 = ns and struct.unpack_from("<I", m, so + 0x0C)[0] & 0x20
        for i in range(nb):
            tr = ob + i * 88 + 0x10
            gseq = struct.unpack_from("<h", m, tr + 2)[0]
            nv, ov = struct.unpack_from("<II", m, tr + 12)
            seqs = range(nv) if gseq >= 0 else ([0] if resident0 else [])
            for s in seqs:
                if s >= nv:
                    continue
                c, at = struct.unpack_from("<II", m, ov + s * 8)
                if c and at + 12 <= len(m):
                    struct.pack_into("<f", m, at, struct.unpack_from("<f", m, at)[0] + 10.0)
                    moved = True
                    break
            if moved:
                break
        if moved:
            open(p, "wb").write(bytes(m))
            planted.append("M8 motion")
            break
    if "M8 motion" not in planted:
        # no model keys a bone inside itself: give one a looping 10 m slide
        import m2model as M
        for rel in shipped:
            p = os.path.join(B, rel)
            body = open(p, "rb").read()
            if M.external_set(body) or struct.unpack_from("<I", body, 0x120)[0] or struct.unpack_from("<I", body, 0x128)[0]:
                continue
            try:
                mm = M.read(body)
                nv, ov = struct.unpack_from("<II", body, 0x3C)
                V = [body[ov + i * 48 + 16] for i in range(nv)]
                bone = max(set(V), key=V.count)
                mm.a["gloops"] = list(mm.a.get("gloops", [])) + [1000]
                old = mm.a["bones"][bone]["trans"]
                mm.a["bones"][bone]["trans"] = M.Track(1, len(mm.a["gloops"]) - 1, [[0, 1000]],
                                                       [[(0.0, 0.0, 0.0), (10.0, 0.0, 0.0)]], old.fmt)
                out, _anims = M.write(mm)
                open(p, "wb").write(out)
                planted.append("M8 motion")
                break
            except Exception:   # noqa: BLE001 - try the next model
                continue
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    subprocess.run([sys.executable, os.path.join(here, "bp.py"), "audit", "--work", W],
                   capture_output=True, text=True)
    got = json.load(open(os.path.join(W, "reports", "audit.json"), encoding="utf-8"))["fails"]
    ok = True
    for g in planted:
        caught = g in got
        ok &= caught
        log("  %-16s %s" % (g, "CAUGHT" if caught else "MISSED  <- the gate is broken: do not trust audit"))
    shutil.rmtree(W, ignore_errors=True)
    if len(planted) < 8:
        log("  (this build has no file of some kinds: %d of 8 defects planted)" % len(planted))
    log("prove-gates: %s" % ("PASS - every planted defect was caught" if ok else "FAIL"))
    return 0 if ok else 1
