r"""
build.py - every selected display -> the files and the row a 3.3.5a client
needs (Tier 1: any stock or custom client, no client mod). Resumable: a
display already built is skipped (--force rebuilds; an unchanged file still
skips). Results: <work>/build/<archive paths>, build/manifest.json and
build/plan.json (display -> row values, items, report).

WHERE EACH SLOT GOES (measured on a 3.3.5a client: Item.dbc InventoryType ->
the folder of its ItemDisplayInfo model, see references/rules.md)
  head      Item\ObjectComponents\Head\<ns>\<stem>_<Prefix><M|F>.m2, one per
            client race and sex (the client appends _<Prefix><M|F>, wow.exe
            0x004E7800). Retail's cut for that race (ComponentModelFileData),
            else retail's ChrRaces model-fallback chain, else an INVISIBLE
            stand-in (a missing file is the client's checker box).
            Placement: + the measured helm shift of that body (measure.py).
  shoulder  Item\ObjectComponents\Shoulder\<ns>\<stem>.m2: ModelName[0] =
            PositionIndex 0 (left), ModelName[1] = PositionIndex 1 (right)
  weapons   Item\ObjectComponents\Weapon\<ns>\ ; shields (InventoryType 14)
            Item\ObjectComponents\Shield\<ns>\
  back      cape texture Item\ObjectComponents\Cape\<ns>\<fdid>.blp + cape geoset
  body      chest/legs/feet/hands/wrist/waist/shirt/tabard: the 8 compositor
            regions Item\TextureComponents\<Region>\<ns>\<name>_<U|M|F>.blp
            (palettised) + GeosetGroup[0..2] remapped to variants this
            client's bodies have (measure.py)
NOT POSSIBLE ON A STOCK CLIENT (reported per display, exported with
--extension for a client mod): retail's 3D chest/leg/boot/glove/bracer/belt/
cape pieces ("collection" models), belt buckles, back quivers.
"""
from __future__ import annotations

import collections
import hashlib
import os
import struct
import time
import traceback

import numpy as np

import bodies as BD
import bpcore
import m2_port as MP
import m2edit as E
import measure as MS
import resolve as RS
import retail_m2 as RM
import scan as SC
from convert import Builder, BAKE_TYPES, baked_tag, texture_kinds

REGION_DIR = ("ArmUpperTexture", "ArmLowerTexture", "HandTexture", "TorsoUpperTexture",
              "TorsoLowerTexture", "LegUpperTexture", "LegLowerTexture", "FootTexture")
BODY_SLOTS = ("chest", "legs", "feet", "hands", "wrist", "waist", "shirt", "tabard")
PIECE_SLOTS = ("chest", "legs", "feet", "hands", "wrist", "waist", "back", "head", "shoulder")
# retail SheatheType -> 3.3.5 (measured on ~26,000 items both games share):
# 0..4 same, 5 (wands) -> 0, 6 -> 6, 7 -> 7, 8 -> 7
SHEATH_335 = {0: 0, 1: 1, 2: 2, 3: 3, 4: 4, 5: 0, 6: 6, 7: 7, 8: 7}
# A slot's geoset groups on an ITEM's own model (WoW Model Viewer WoWItem.cpp +
# retail DB2, measured on every Midnight display): group -> AGG column
SLOT_GROUPS = {"head": ((27, 0), (21, 1)), "shoulder": ((26, 0),),
               "chest": ((8, 0), (10, 1), (13, 2), (22, 3), (28, 4)),
               "shirt": ((8, 0), (10, 1)), "legs": ((11, 0), (9, 1), (13, 2)),
               "hands": ((4, 0), (23, 1)), "feet": ((5, 0), (20, 1)),
               "waist": ((18, 0),), "back": ((15, 0),), "tabard": ((12, 0),), "wrist": ()}


class Retail:
    """Lazy whole-retail indexes the per-display rules need."""

    def __init__(self, prj):
        self.prj = prj
        self.db = RS.Db2(prj)
        self._slots_of_res = None
        self._idi_by_res = None
        self._tfd = None
        self._ctfd_race = None
        self._mmr = None

    def slots_of_res(self) -> dict:
        """ModelResourcesID -> the set of slots whose displays use it (a model
        used by several slots is a COMBINED set model: each slot keeps its part)."""
        if self._slots_of_res is None:
            slot_of_disp = {}
            for r in self.db.table("ItemAppearance", ("ItemDisplayInfoID", "DisplayType")):
                slot_of_disp.setdefault(r["ItemDisplayInfoID"], RS.SLOT.get(int(r["DisplayType"] or 0), "?"))
            out = collections.defaultdict(set)
            for r in self.db.table("ItemDisplayInfo", ("ID", "ModelResourcesID_0", "ModelResourcesID_1")):
                s = slot_of_disp.get(r["ID"])
                if not s:
                    continue
                for k in (0, 1):
                    v = r["ModelResourcesID_%d" % k]
                    if v and v != "0":
                        out[v].add(s)
            self._slots_of_res = out
        return self._slots_of_res

    def multi_slot(self, res: str) -> bool:
        return len(self.slots_of_res().get(res, ())) > 1

    def supplies(self, res: str, k: int, t2: int) -> dict:
        """{type: Counter(fdid)}: what the displays wearing model resource `res`
        (as model k) with the same type-2 texture hand its retail-only slots."""
        db = self.db
        if self._idi_by_res is None:
            self._idi_by_res = collections.defaultdict(list)
            for r in db.table("ItemDisplayInfo", ("ID", "ModelResourcesID_0", "ModelResourcesID_1",
                                                   "ModelMaterialResourcesID_0", "ModelMaterialResourcesID_1")):
                for kk in (0, 1):
                    v = r["ModelResourcesID_%d" % kk]
                    if v and v != "0":
                        self._idi_by_res[(v, kk)].append((r["ID"], r["ModelMaterialResourcesID_%d" % kk]))
            self._tfd = collections.defaultdict(list)
            for r in db.table("TextureFileData", ("FileDataID", "MaterialResourcesID")):
                self._tfd[r["MaterialResourcesID"]].append(int(r["FileDataID"]))
            self._ctfd_race = {int(r["ID"]): int(r["RaceID"] or 0)
                               for r in db.table("ComponentTextureFileData", ("ID", "RaceID"))}
            self._mmr = collections.defaultdict(list)
            for r in db.table("ItemDisplayInfoModelMatRes", ("ItemDisplayInfoID", "MaterialResourcesID",
                                                              "TextureType", "ModelIndex")):
                self._mmr[r["ItemDisplayInfoID"]].append(r)
        out = collections.defaultdict(collections.Counter)

        def race0(mat):
            return [f for f in self._tfd.get(mat, []) if self._ctfd_race.get(f, 0) == 0]
        for did, mat in self._idi_by_res.get((res, k), []):
            if t2 not in race0(mat):
                continue
            for mm in self._mmr.get(did, []):
                ty = int(mm["TextureType"])
                if ty in BAKE_TYPES and int(mm["ModelIndex"]) == k:
                    for f in race0(mm["MaterialResourcesID"])[:1]:
                        out[ty][f] += 1
        return out


class DisplayBuilder:
    def __init__(self, prj, log):
        self.prj = prj
        self.log = log
        self.b = Builder(prj)
        self.retail = Retail(prj)
        self.scan = SC.load(prj)
        self.ns = prj.ns
        self._geosets = {}
        self._used = {}

    # --- texture slots ---------------------------------------------------
    def item_texture(self, e, k: int):
        for t in e.get("model_tex", []):
            if t["k"] == k and t["race"] == 0:
                return t["fdid"]
        for t in e.get("extra_tex", []):
            if t["model"] == k and t["type"] == 2 and t["race"] == 0:
                return t["fdid"]
        return None

    def own_baked(self, e, k: int) -> dict:
        out = {}
        for t in e.get("extra_tex", []):
            if t["model"] == k and t["type"] in BAKE_TYPES and t["race"] == 0:
                out.setdefault(t["type"], t["fdid"])
        return out

    def used_types(self, fdid: int, keep, tag: str) -> set:
        key = (fdid, tag)
        if key not in self._used:
            raw = self.b.files.read(fdid)
            body = MP.unwrap(raw)[0]
            sids = RM.deps(raw).get("skin") or []
            out = set()
            if sids:
                sk = RM.skin(self.b.files.read(sids[0]))
                kinds = texture_kinds(body)
                nl, ol = struct.unpack_from("<II", body, 0x80)
                look = struct.unpack_from("<%dh" % nl, body, ol) if nl else ()
                for bt in sk["batches"]:
                    if keep and not keep(sk["subs"][bt[3]]["id"]):
                        continue
                    for u in range(bt[8]):
                        if bt[9] + u < len(look) and 0 <= look[bt[9] + u] < len(kinds):
                            out.add(kinds[look[bt[9] + u]])
            self._used[key] = out
        return self._used[key]

    def baked_for(self, e, k: int) -> dict:
        """{type: fdid} for retail-only slots 3/4/24 of model k: the display's
        own, completed from displays wearing the same model in the same
        colourway (a combined set model's slot displays each hand only some
        slots - a missing one drew a white sheet)."""
        out = self.own_baked(e, k)
        t2 = self.item_texture(e, k)
        ms = [m for m in e.get("models", []) if m["k"] == k]
        if not t2 or not ms:
            return out
        tag, keep = self.slot_filter(e, k)
        try:
            need = self.used_types(ms[0]["fdid"], keep, tag) & set(BAKE_TYPES)
        except Exception:      # noqa: BLE001
            return out
        missing = sorted(need - set(out))
        if missing:
            sup = self.retail.supplies(e["row"].get("ModelResourcesID_%d" % k, "0"), k, t2)
            for ty in missing:
                if sup.get(ty):
                    out[ty] = sup[ty].most_common(1)[0][0]
        return out

    # --- geosets of the item's own model ---------------------------------
    def model_geosets(self, fdid: int) -> set:
        if fdid not in self._geosets:
            try:
                raw = self.b.files.read(fdid)
                sids = RM.deps(raw).get("skin") or []
                self._geosets[fdid] = {s["id"] for s in RM.skin(self.b.files.read(sids[0]))["subs"]} if sids else set()
            except Exception:      # noqa: BLE001
                self._geosets[fdid] = set()
        return self._geosets[fdid]

    def attachment_want(self, e, slot: str) -> dict:
        """Attachment model (helm/shoulder/buckle): group shows x01 +
        max(AGG[col], GeosetGroupOverride) (158 of 158 measured: the override
        marks the top-tier / Gladiator looks whose enhanced x02 part shows)."""
        ov = max(0, int(e["row"].get("GeosetGroupOverride", "0") or 0))
        agg = [int(e["row"]["AttachmentGeosetGroup_%d" % i]) for i in range(6)]
        return {g: g * 100 + 1 + max(agg[c], ov) for g, c in SLOT_GROUPS.get(slot, ()) if agg[c] >= 0}

    def slot_filter(self, e, k: int):
        """(tag, keep(submesh id)) for model k. Combined set model: only this
        slot's groups at group*100 + 1 + AGG (1,125 of 1,125 measured); a model
        one slot uses: attachment_want. Ungrouped geometry (< 100) always draws."""
        res = e["row"].get("ModelResourcesID_%d" % k, "0")
        multi = self.retail.multi_slot(res)
        slot = e["slot"]
        agg = [int(e["row"]["AttachmentGeosetGroup_%d" % i]) for i in range(6)]
        if multi:
            want = {g: g * 100 + 1 + agg[c] for g, c in SLOT_GROUPS.get(slot, ()) if agg[c] >= 0}
        else:
            want = self.attachment_want(e, slot)
        tag = "%s:%s:%d" % (slot, ",".join("%d" % want[g] for g in sorted(want)), int(multi))

        def keep(sid: int) -> bool:
            if sid < 100:
                return True
            g = sid // 100
            if g in want:
                return want[g] == sid
            return not multi
        return tag, keep

    def filter_model(self, model: bytes, skin: bytes, keep):
        sk = RM.skin(skin)
        tris = E.triangles(model, skin)
        ids = [s["id"] for s in sk["subs"]]
        sel = {i for i, (si, _abc) in enumerate(tris) if keep(ids[si])}
        if not sel:
            return None
        return E.subset(model, skin, sel)

    def filtered(self, rel: str, keep, tag: str, kind: str) -> str:
        """A copy of built model `rel` with only the submeshes `keep` accepts
        ('' when nothing is left)."""
        out = rel[:-3] + "__s" + hashlib.sha1(tag.encode()).hexdigest()[:6] + ".m2"
        if self.b.have(out):
            return out
        got = self.filter_model(self.b.read_built(rel), self.b.read_built(rel[:-3] + "00.skin"), keep)
        if got is None:
            return ""
        m2, s2 = E.repack(got[0]), got[1]
        MP.check_264(m2, [s2])
        src = dict(self.b.man[rel.lower()].get("src") or {})
        src["filtered_from"] = rel
        self.b.write(out, m2, kind, src)
        self.b.write(out[:-3] + "00.skin", s2, kind + "-skin", {})
        self.b.copy_anims(rel, out)
        return out

    # --- race-agnostic models (weapons, shields, shoulders) --------------
    def agnostic(self, e, k: int, folder: str, pos: int = -1, slot: str = ""):
        ms = [m for m in e.get("models", []) if m["k"] == k and m["race"] == 0
              and (pos < 0 or m["pos"] in (pos, -1))]
        if pos >= 0:
            ms.sort(key=lambda m: m["pos"] != pos)
        if not ms:
            return "", ""
        f = ms[0]["fdid"]
        baked = self.baked_for(e, k)
        pc = self.b.effective_pc(f, e["row"].get("ParticleColorID"))
        stem = self.b.stem(f) + baked_tag(baked, pc)
        rel = "Item\\ObjectComponents\\%s\\%s\\%s.m2" % (folder, self.ns, stem)
        self.b.convert_model(f, rel, baked, kind=folder.lower(), particle_color=pc)
        if slot:
            want = self.attachment_want(e, slot)
            gs = self.model_geosets(f)
            if any(x // 100 in want and x != want[x // 100] for x in gs):
                tag = "%s:%s:a" % (slot, ",".join("%d" % want[g] for g in sorted(want)))
                rel = self.filtered(rel, lambda sid: sid < 100 or sid // 100 not in want or want[sid // 100] == sid,
                                    tag, folder.lower())
                if not rel:
                    return "", ""
        stem = rel.rsplit("\\", 1)[1][:-3]
        tex = self.item_texture(e, k)
        return "%s\\%s.mdx" % (self.ns, stem), (self.b.row_texture(tex, folder) if tex else "")

    # --- helms -------------------------------------------------------------
    def cut_for(self, models, race: int, sex: int):
        have = {}
        for m in models:
            if m["race"]:
                for s in ((0, 1) if m["gender"] not in (0, 1) else (m["gender"],)):
                    have.setdefault((m["race"], s), m["fdid"])
        seen, cur = set(), (race, sex)
        while cur and cur not in seen:
            seen.add(cur)
            if cur in have:
                return have[cur], cur[0], cur[1]
            cur = RS.fallback_of(self.prj, *cur)
        return None

    def build_helm(self, e, report) -> str:
        k = 0
        models = [m for m in e.get("models", []) if m["k"] == k]
        if not models:
            return ""
        baked = self.baked_for(e, k)
        pc = int(e["row"].get("ParticleColorID") or 0)
        pc = pc if pc and any(self.b.uses_particle_color(m["fdid"]) for m in models[:4]) else 0
        tag = baked_tag(baked, pc)
        any_cut = next((m["fdid"] for m in models if m["race"]), models[0]["fdid"])
        ftag, keep = self.slot_filter(e, k)
        hstem = strip_race(self.b.stem(any_cut)) + tag + "__s" + hashlib.sha1(ftag.encode()).hexdigest()[:6]
        folder = "Item\\ObjectComponents\\Head\\%s\\" % self.ns
        built, missing = collections.Counter(), []
        agnostic = [m for m in models if m["race"] == 0]
        for race in self.scan["races"]:
            for sx in race["bodies"]:
                sex = "MF".index(sx)
                rel = folder + "%s_%s%s.m2" % (hstem, race["prefix"], sx)
                if self.b.have(rel):
                    built["kept"] += 1
                    continue
                got = None
                if race.get("retail") is not None:
                    got = self.cut_for(models, race["retail"], sex)
                if not got and agnostic:
                    got = (agnostic[0]["fdid"], race.get("retail"), sex)
                if not got:
                    missing.append(rel)
                    built["no retail cut for this race (invisible stand-in)"] += 1
                    continue
                src = "Item\\ObjectComponents\\Head\\%s\\src\\%d%s.m2" % (self.ns, got[0], tag)
                self.b.convert_model(got[0], src, baked, kind="helm-src", particle_color=pc)
                raw, sk = self.b.read_built(src), self.b.read_built(src[:-3] + "00.skin")
                if keep:
                    got_f = self.filter_model(raw, sk, keep)
                    if got_f is None:
                        missing.append(rel)
                        built["model has no head part (invisible stand-in)"] += 1
                        continue
                    raw, sk = got_f
                if is_body_space(raw):
                    raw, sk = self.head_local(raw, sk, got[1], got[2])
                    report["body_space_helms"] = report.get("body_space_helms", 0) + 1
                shift = MS.shift_for(self.prj, race["prefix"], sx)
                if shift is None:
                    raise RuntimeError("no helm shift for %s%s: run measure" % (race["prefix"], sx))
                raw = shift_helm(raw, shift)
                raw = E.repack(raw)
                MP.check_264(raw, [sk])
                self.b.write(rel, raw, "helm", {"from": got[0], "src_race": got[1], "src_sex": got[2],
                                                "shift": shift})
                self.b.write(rel[:-3] + "00.skin", sk, "helm-skin", {})
                self.b.copy_anims(src, rel)
                built["built"] += 1
        self.write_invisible(folder, hstem, missing)
        report["helm_files"] = dict(built)
        return "%s\\%s.mdx" % (self.ns, hstem)

    def head_local(self, model: bytes, skin: bytes, race, sex):
        """A helm authored on the character's bones (body space) made a helm
        point model: one bone, every vertex minus retail's helm point."""
        rb = BD.retail_body(self.prj, race, sex) if race is not None else None
        if rb is None or rb.helm_point() is None:
            raise RuntimeError("body-space helm, but retail body %s/%s has no helm point" % (race, sex))
        m, s = E.single_bone(model, skin)
        return E.fit_bounds(E.translate(m, -rb.helm_point())), s

    def write_invisible(self, folder: str, hstem: str, rels) -> None:
        rels = [r for r in rels if not self.b.have(r)]
        if not rels:
            return
        src = next((v["path"] for k, v in self.b.man.items()
                    if k.startswith((folder + hstem + "_").lower()) and k.endswith(".m2") and v["kind"] == "helm"), None)
        if not src:
            src = next((v["path"] for k, v in self.b.man.items()
                        if k.startswith((folder + "src\\").lower()) and k.endswith(".m2")), None)
        if not src:
            return
        body = bytearray(self.b.read_built(src))
        skin = self.b.read_built(src[:-3] + "00.skin")
        nv, ov = struct.unpack_from("<II", body, 0x3C)
        for i in range(nv):
            struct.pack_into("<3f", body, ov + i * 48, 0.0, 0.0, 0.0)
        struct.pack_into("<II", body, 0x120, 0, 0)
        struct.pack_into("<II", body, 0x128, 0, 0)
        body = E.repack(bytes(body))
        for rel in rels:
            self.b.write(rel, body, "helm-invisible", {"why": "retail cuts this helm for no such race"})
            self.b.write(rel[:-3] + "00.skin", skin, "helm-skin", {})
            self.b.copy_anims(src, rel)

    # --- body textures ---------------------------------------------------
    def sections(self, e, report) -> list[str]:
        cols = [""] * 8
        by = collections.defaultdict(dict)
        for s in e.get("sections", []):
            if s["race"] != 0:
                report.setdefault("race_specific_sections_not_carried", set()).add(s["section"])
                continue
            if s["section"] > 7:
                report.setdefault("sections_not_carried", set()).add(s["section"])
                continue
            g = s["gender"]
            by[s["section"]]["U" if g not in (0, 1) else "MF"[g]] = s["fdid"]
        for sec, var in sorted(by.items()):
            base = "_".join(str(var[x]) for x in ("U", "M", "F") if x in var)
            name = "%s\\%s" % (self.ns, base)
            for suf, fid in var.items():
                self.b.section_texture(fid, "Item\\TextureComponents\\%s\\%s_%s.blp" % (REGION_DIR[sec], name, suf))
            if "M" in var and "F" not in var and "U" not in var:
                # the client asks _F then _U on a female body: give it the male art as _U
                self.b.section_texture(var["M"], "Item\\TextureComponents\\%s\\%s_U.blp" % (REGION_DIR[sec], name))
            if "F" in var and "M" not in var and "U" not in var:
                self.b.section_texture(var["F"], "Item\\TextureComponents\\%s\\%s_U.blp" % (REGION_DIR[sec], name))
            cols[sec] = name
        for k in ("race_specific_sections_not_carried", "sections_not_carried"):
            if k in report:
                report[k] = sorted(report[k])
        return cols

    # --- one display -----------------------------------------------------
    def build(self, d: str, e: dict) -> dict:
        r = e["row"]
        slot = e["slot"]
        it = e["items"][0] if e.get("items") else {}
        report = {}
        row = {"model": ["", ""], "tex": ["", ""], "icon": "", "regions": [""] * 8,
               "gg": [int(r["GeosetGroup_%d" % i]) for i in range(6)],
               "gg335": [0, 0, 0], "helmvis": [int(r["HelmetGeosetVis_0"]), int(r["HelmetGeosetVis_1"])],
               "flags": int(r["Flags"]), "itemvisual": int(r["ItemVisual"]),
               "particlecolor": int(r["ParticleColorID"]), "sheath_matrix": int(r["SheatheTransformMatrixID"])}
        out = {"slot": slot, "items": e.get("items", []), "sets": e.get("sets", []), "row": row,
               "report": report, "inv": it.get("inv", 0), "class": it.get("class", 0), "sub": it.get("sub", 0),
               "sheath": SHEATH_335.get(it.get("sheathe", 0), 0), "material": it.get("material", 0)}
        if slot in RS.WEAPON_SLOTS:
            folder = "Shield" if out["inv"] == 14 else "Weapon"
            row["model"][0], row["tex"][0] = self.agnostic(e, 0, folder)
            if any(m["k"] == 1 for m in e.get("models", [])):
                row["model"][1], row["tex"][1] = self.agnostic(e, 1, folder)
            if not row["model"][0]:
                report["error"] = "weapon display with no model"
        elif slot == "shoulder":
            row["model"][0], row["tex"][0] = self.agnostic(e, 0, "Shoulder", pos=0, slot="shoulder")
            row["model"][1], row["tex"][1] = self.agnostic(e, 1, "Shoulder", pos=1, slot="shoulder")
            if not row["model"][1]:
                row["model"][1], row["tex"][1] = self.agnostic(e, 0, "Shoulder", pos=1, slot="shoulder")
            if not row["model"][0] and not row["model"][1]:
                report["tier2_only"] = "shoulder display with no race-agnostic model"
        elif slot == "head":
            row["model"][0] = self.build_helm(e, report)
            tex = self.item_texture(e, 0)
            if tex:
                row["tex"][0] = self.b.row_texture(tex, "Head")
        elif slot == "back":
            tex = self.item_texture(e, 0) or self.item_texture(e, 1)
            if tex:
                row["tex"][0] = self.b.row_texture(tex, "Cape")
            if any(m["race"] for m in e.get("models", [])):
                report["tier2_only"] = "retail 3D cape pieces (shown as the texture cape on a stock client)"
        elif slot == "quiver":
            report["error"] = "back-slot quiver: no 3.3.5 equivalent (Tier 2 only)"
        if slot in BODY_SLOTS:
            row["regions"] = self.sections(e, report)
            if any(m["race"] for m in e.get("models", [])) or (slot == "waist" and e.get("models")):
                report["tier2_only"] = "retail 3D %s pieces (stock clients show the textures and geosets only)" % slot
        # body geosets this client's bodies have (GeosetGroup[0..2])
        groups = {g: c for g, c in SLOT_GROUPS.get(slot, ())}
        for col in range(3):
            v = max(0, row["gg"][col])
            g = next((gg for gg, cc in groups.items() if cc == col), None)
            row["gg335"][col] = MS.client_value(self.prj, g, v) if g and slot not in ("head", "shoulder") else v
        if e.get("icon"):
            row["icon"] = self.b.icon(e["icon"])
        if row["itemvisual"]:
            report["itemvisual_not_carried"] = row["itemvisual"]
        if not (row["model"][0] or row["model"][1] or row["tex"][0] or any(row["regions"])) and "error" not in report:
            report["error"] = "nothing a 3.3.5 client can draw for this display"
        return out


def strip_race(stem: str) -> str:
    p = stem.rsplit("_", 2)
    if len(p) == 3 and len(p[1]) == 2 and p[2] in ("m", "f"):
        return p[0]
    return stem


def is_body_space(model: bytes) -> bool:
    P = E.positions(model)
    return len(P) > 0 and float(np.median(P[:, 2])) > 0.9


def shift_helm(model: bytes, d) -> bytes:
    m = bytearray(E.translate(model, d))
    dv = np.asarray(d, dtype=np.float64)

    def move(at):
        lo = np.array(struct.unpack_from("<3f", m, at)) + dv
        hi = np.array(struct.unpack_from("<3f", m, at + 12)) + dv
        rad = float(np.linalg.norm(np.maximum(np.abs(lo), np.abs(hi))))
        struct.pack_into("<7f", m, at, *lo, *hi, rad)
    for base in E.BOUNDS:
        move(base)
    ns, so = struct.unpack_from("<II", m, 0x1C)
    for i in range(ns):
        move(so + i * 64 + 0x20)
    return E.union_bounds(bytes(m))[0]


def export_extension(prj, log, sel, plan) -> None:
    """Tier 2: retail's per-race 3D pieces and buckles, converted, with a
    manifest a client mod can bind (references/client-extension.md)."""
    db = DisplayBuilder(prj, log)
    sc = db.scan
    man = {}
    for d, e in sorted(sel["displays"].items(), key=lambda x: int(x[0])):
        if e["slot"] not in PIECE_SLOTS or e["slot"] in ("head", "shoulder") or not e.get("row"):
            continue
        pieces = []
        for k in (0, 1):
            models = [m for m in e.get("models", []) if m["k"] == k]
            if not models:
                continue
            tag, keep = db.slot_filter(e, k)
            baked = db.baked_for(e, k)
            for race in sc["races"]:
                for sx in race["bodies"]:
                    got = db.cut_for(models, race["retail"], "MF".index(sx)) if race.get("retail") is not None else None
                    if not got and not any(m["race"] for m in models):
                        got = (models[0]["fdid"], 0, 0)
                    if not got:
                        continue
                    rel = "Item\\ObjectComponents\\Collections\\%s\\%s%s_%s%s.m2" % (
                        db.ns, db.b.stem(got[0]), baked_tag(baked), race["prefix"], sx)
                    try:
                        src = rel[:-3] + "__src.m2"
                        db.b.convert_model(got[0], src, baked, kind="piece-src")
                        f = db.filtered(src, keep, tag, "piece")
                    except Exception as ex:   # noqa: BLE001
                        pieces.append({"race": race["id"], "sex": sx, "error": str(ex)})
                        continue
                    if f:
                        pieces.append({"race": race["id"], "prefix": race["prefix"], "sex": sx, "k": k,
                                       "model": f, "retail_race": got[1], "retail_sex": got[2],
                                       "skinned_to_retail_bones": bool(got[1])})
        if pieces:
            man[d] = {"slot": e["slot"], "new_display": plan.get(d, {}).get("display_id"), "pieces": pieces}
    db.b.save()
    bpcore.save_json(prj.path("out", "extension", "extension.json"), man)
    log("extension: %d displays with Tier-2 pieces -> out/extension/extension.json" % len(man))


def run(prj, log, only: set, force: bool, extension: bool) -> int:
    sel = bpcore.load_json(prj.path("selection.json"))
    plan_path = prj.path("build", "plan.json")
    plan = bpcore.load_json(plan_path, {})
    enc = set(bpcore.load_json(prj.path("reports", "fetch.json"), {}).get("encrypted", []))
    db = DisplayBuilder(prj, log)
    todo = sorted(sel["displays"], key=int)
    if only:
        todo = [d for d in todo if d in only]
    t0 = time.time()
    done = err = skip = 0
    for i, d in enumerate(todo, 1):
        e = sel["displays"][d]
        if d in plan and plan[d].get("ok") and not force and not only:
            skip += 1
            continue
        if not e.get("row"):
            plan[d] = {"slot": e["slot"], "ok": False, "error": "no ItemDisplayInfo row at this build"}
            err += 1
            continue
        files = {m["fdid"] for m in e.get("models", [])} | {t["fdid"] for t in e.get("model_tex", [])} | \
            {t["fdid"] for t in e.get("sections", [])}
        if files & enc:
            plan[d] = {"slot": e["slot"], "ok": False,
                       "error": "Encrypted: uses files retail has not released at this build"}
            err += 1
            continue
        try:
            plan[d] = db.build(d, e)
            plan[d]["ok"] = "error" not in plan[d]["report"]
            if not plan[d]["ok"]:
                plan[d]["error"] = plan[d]["report"]["error"]
            done += 1
        except Exception as ex:   # noqa: BLE001 - recorded per display, never fatal for the run
            plan[d] = {"slot": e["slot"], "ok": False, "error": "%s: %s" % (type(ex).__name__, ex),
                       "trace": traceback.format_exc()[-2000:]}
            err += 1
        if i % 10 == 0 or i == len(todo):
            db.b.save()
            bpcore.save_json(plan_path, plan)
            log("  %d/%d displays (%d built, %d errors, %d already done) %.0fs"
                % (i, len(todo), done, err, skip, time.time() - t0))
    db.b.save()
    bpcore.save_json(plan_path, plan)
    errors = collections.Counter(v.get("error", "").split(":")[0] for v in plan.values() if not v.get("ok"))
    log("build: %d built, %d errors, %d skipped (already built) in %.0fs" % (done, err, skip, time.time() - t0))
    for k, n in errors.most_common():
        log("  %4d  %s" % (n, k))
    tier2 = sum(1 for v in plan.values() if v.get("report", {}).get("tier2_only"))
    if tier2:
        log("  %d displays have retail 3D parts a stock client cannot show (see reports; --extension exports them)" % tier2)
    if extension:
        export_extension(prj, log, sel, plan)
    log("next: python bp.py tables --work <W>")
    bad = [k for k in errors if not k.startswith("Encrypted") and k]
    return 1 if bad else 0
