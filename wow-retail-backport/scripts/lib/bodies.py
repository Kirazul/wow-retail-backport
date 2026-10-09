"""
bodies.py - character bodies: retail's (by race/sex, through retail's own
tables) and the target client's (from its archives, as scan.json found them).

  retail_body(prj, race, sex)  -> Body | None
  client_body(prj, race_entry, "M"|"F") -> Body | None

A Body gives bind-pose vertex positions, geoset (submesh) meshes, bone
pivots, the head key bone and the attachment points (11 = helm). Retail
bodies keep their bones and attachments in a separate .skel since
Shadowlands; m2model.read_retail merges it, so both kinds read the same.

Retail body file: ChrRaceXChrModel (ChrRacesID, Sex) -> ChrModel.DisplayID ->
CreatureDisplayInfo.ModelID -> CreatureModelData.FileDataID.
"""
from __future__ import annotations

import collections

import numpy as np

import m2model as M
import retail_m2 as RM
from client import Client
from wago import Db2, Files

KEYBONE_HEAD = 6        # M2 key bone enum: 6 = Head


class Body:
    def __init__(self, name: str, md20_or_raw: bytes, skin: bytes, skel: bytes | None = None, files=None):
        self.name = name
        md20_or_raw = _no_effects(md20_or_raw)
        if md20_or_raw[:4] == b"MD20" and skel is None:
            m = M.read(md20_or_raw)
        else:
            # bind pose only: no animation file is read (an NPC body's .anim can be
            # another layout entirely, and nothing here plays an animation)
            m = M.read_retail(md20_or_raw, skel, lambda f: None)
        md = RM.md20(md20_or_raw)
        self.pos = np.array([v[0] for v in RM.vertices(md)], dtype=np.float64)
        sk = RM.skin(skin)
        self.parts = collections.defaultdict(set)
        for s in sk["subs"]:
            self.parts[s["id"]].update(sk["lookup"][i] for i in sk["tris"][s["istart"]:s["istart"] + s["icount"]])
        self.geosets = sorted(self.parts)
        self.pivots = [np.array(b["pivot"], dtype=np.float64) for b in m.a["bones"]]
        self.keybones = list(m.a.get("keybones", []))
        self.attach = {a["id"]: (a["bone"], np.array(a["pos"], dtype=np.float64)) for a in m.a.get("attach", [])}

    def part(self, gid: int) -> np.ndarray:
        ix = sorted(self.parts.get(gid, ()))
        return self.pos[ix] if ix else np.zeros((0, 3))

    def head_pivot(self):
        kb = self.keybones
        if len(kb) > KEYBONE_HEAD and 0 <= kb[KEYBONE_HEAD] < len(self.pivots):
            return self.pivots[kb[KEYBONE_HEAD]]
        return None

    def helm_point(self):
        a = self.attach.get(11)
        return None if a is None else a[1]


def _no_effects(raw: bytes) -> bytes:
    """The same model with no ribbons and no particle emitters (a body is
    read for its mesh, bones and attachments; the reader does not parse
    effect records)."""
    import struct
    base = 0
    if raw[:4] != b"MD20":
        c = RM.chunks(raw)
        if "MD21" not in c:
            return raw
        base = raw.index(c["MD21"][0])
    b = bytearray(raw)
    struct.pack_into("<IIII", b, base + 0x120, 0, 0, 0, 0)
    return bytes(b)


_RB = {}


def retail_body_fdid(prj, race: int, sex: int):
    if prj.build not in _RB:
        db = Db2(prj)
        cm = {r["ID"]: r for r in db.table("ChrModel", ("ID", "DisplayID"))}
        cdi = {r["ID"]: r["ModelID"] for r in db.table("CreatureDisplayInfo", ("ID", "ModelID"))}
        cmd = {r["ID"]: int(r["FileDataID"] or 0) for r in db.table("CreatureModelData", ("ID", "FileDataID"))}
        out = {}
        for r in db.table("ChrRaceXChrModel", ("ChrRacesID", "ChrModelID", "Sex")):
            m = cm.get(r["ChrModelID"])
            if not m:
                continue
            f = cmd.get(cdi.get(m["DisplayID"], ""), 0)
            if f:
                out.setdefault((int(r["ChrRacesID"]), int(r["Sex"])), f)
        _RB[prj.build] = out
    return _RB[prj.build].get((race, sex))


def retail_body(prj, race: int, sex: int):
    f = retail_body_fdid(prj, race, sex)
    if not f:
        return None
    F = Files(prj)
    try:
        raw = F.read(f)
        d = RM.deps(raw)
        skin = F.read(d["skin"][0])
        skel = F.read(d["skel"][0]) if d.get("skel") else None
        return Body("retail %d/%d (%d)" % (race, sex, f), raw, skin, skel, F)
    except Exception as ex:   # noqa: BLE001 - "no usable retail body" is an answer the callers report
        print("  (retail body %d/%d unreadable: %s)" % (race, sex, ex))
        return None


_CLIENT = {}


def client(prj) -> Client:
    k = prj.cfg["client_data"]
    if k not in _CLIENT:
        _CLIENT[k] = Client(k, prj.cfg.get("locale") or None, bool(prj.cfg.get("wildcard_patches")))
    return _CLIENT[k]


def client_body(prj, race: dict, sx: str):
    info = race["bodies"].get(sx)
    if not info:
        return None
    c = client(prj)
    d = c.read(info["path"])
    sk = c.read(info["path"][:-3] + "00.skin")
    if not d or not sk:
        return None
    return Body("client %s%s" % (race["prefix"], sx), d, sk)
