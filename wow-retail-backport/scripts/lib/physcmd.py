r"""
physcmd.py - `bp.py phys`: retail physics (.phys) of any model, read and
simulated - for study, for the report, and for the rest-pose bake.

  python bp.py phys --work W <model fdid>            readable dump of bodies, shapes, joints
  python bp.py phys --work W <model fdid> --settle   settle it at rest (gravity, joints,
                                                     contacts with the wearer stand-ins)
  python bp.py phys --work W --survey                settle EVERY physics model of the
                                                     selection: rest, joint error, contacts

What a 3.3.5 client can show of retail physics: references/rules.md "Physics".
"""
from __future__ import annotations

import collections
import json

import numpy as np

import bpcore
import physbake as PB
import physread as PH
import retail_m2 as RM
from wago import Files


def settle_report(phys) -> dict:
    pose, err, rest, worst, drift = PB.settle(phys)
    bodies = [b for b in phys["bodies"] if b["type"] == 1]
    moved = [float(np.linalg.norm(pose[b["bone"]][1] - b["pos"])) for b in bodies if b["bone"] in pose]
    # contact check at rest: deepest remaining penetration into a stand-in
    return {"dynamic_bodies": len(bodies), "joint_error": round(err, 4), "rest_after_s": round(rest, 2),
            "chatter": round(worst, 5), "drift": round(drift, 5),
            "max_rest_offset": round(max(moved), 3) if moved else 0.0,
            "settled": bool(worst <= 1e-3 and drift <= 2e-2 and err <= 0.02)}


def run(prj, log, fdid: int | None, settle: bool, survey: bool) -> int:
    F = Files(prj)
    if fdid:
        blob = PH.physics_blob(F, fdid)
        if not blob:
            log("model %d has no physics (no PFID / PFDC chunk)" % fdid)
            return 0
        p = PH.parse(blob)
        for line in PH.dump(p):
            log(line)
        if settle:
            log(json.dumps(settle_report(p), indent=1))
        return 0
    if survey:
        sel = bpcore.load_json(prj.path("selection.json"))
        fids = sorted({m["fdid"] for e in sel["displays"].values() for m in e.get("models", [])})
        out, c = {}, collections.Counter()
        for f in fids:
            raw = F.try_read(f)
            if not raw:
                continue
            ch = RM.chunks(raw)
            if "PFID" not in ch and "PFDC" not in ch:
                continue
            try:
                r = settle_report(PH.parse(PH.physics_blob(F, f)))
            except Exception as ex:   # noqa: BLE001 - reported per model
                r = {"error": str(ex)}
            out[str(f)] = r
            c["settled" if r.get("settled") else ("error" if "error" in r else "not settled")] += 1
        bpcore.save_json(prj.path("reports", "physics.json"), out)
        log("physics survey: %d models with physics: %s -> reports/physics.json" % (len(out), dict(c)))
        return 0
    log("give a model fdid, or --survey")
    return 2
