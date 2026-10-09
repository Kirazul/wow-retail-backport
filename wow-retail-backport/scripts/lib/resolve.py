"""
resolve.py - what to backport, resolved through retail's own tables.

  find "text"          search ItemSparse names and TransmogSet names
  select ...           sets / items / appearances / displays / an expansion
                       -> <work>/selection.json
  fetch                every retail file the selection needs (and what those
                       files name: skins, textures, .anim, .skel, .phys,
                       particle and recursive models), one build only

THE GRAPH (wowdev names, every link a DB2 row at the pinned build)
  TransmogSet -> TransmogSetItem -> ItemModifiedAppearance (ItemID, ItemAppearanceID)
  ItemAppearance (DisplayType = slot, ItemDisplayInfoID, DefaultIconFileDataID)
  ItemDisplayInfo (ModelResourcesID[2], ModelMaterialResourcesID[2],
      GeosetGroup[6], AttachmentGeosetGroup[6], GeosetGroupOverride,
      HelmetGeosetVis[2], Flags, ItemVisual, ParticleColorID,
      SheatheTransformMatrixID)
    ModelResourcesID         -> ModelFileData -> ComponentModelFileData
                                (RaceID, GenderIndex, ClassID, PositionIndex)
    ModelMaterialResourcesID -> TextureFileData -> ComponentTextureFileData
    ItemDisplayInfoModelMatRes (TextureType, ModelIndex) = extra model textures
    ItemDisplayInfoMaterialRes (ComponentSection) = body (skin) textures
  Item (ClassID, SubclassID, InventoryType, SheatheType, Material, IconFileDataID)
  ItemSparse (Display_lang, OverallQualityID, ExpansionID, ItemNameDescriptionID)

A display's files are recorded for EVERY retail race (the build decides which
bodies wear which file); `fetch` downloads only the races the client needs
(scan.json) plus their ChrRaces model-fallback chain and race 0 (agnostic).
"""
from __future__ import annotations

import collections
import re
import time

import bpcore
import retail_m2 as RM
import scan as SC
from wago import Db2, Files, Encrypted, Missing

# ItemAppearance.DisplayType -> slot (retail enum; equals the 3.3.5 component
# slot order for armour: 0 head .. 10 tabard)
SLOT = {0: "head", 1: "shoulder", 2: "shirt", 3: "chest", 4: "waist", 5: "legs",
        6: "feet", 7: "wrist", 8: "hands", 9: "back", 10: "tabard",
        11: "mainhand", 12: "offhand", 13: "ranged", 14: "quiver", 15: "held"}
WEAPON_SLOTS = {"mainhand", "offhand", "ranged", "held"}


def find(prj, log, text: str, limit: int) -> int:
    db = Db2(prj)
    rx = re.compile(re.escape(text), re.I)
    sets = [r for r in db.table("TransmogSet", ("ID", "Name_lang", "ExpansionID", "ClassMask",
                                                  "ParentTransmogSetID"))
            if rx.search(r["Name_lang"])]
    log("sets matching %r: %d" % (text, len(sets)))
    for r in sets[:limit]:
        log("  set %6s  exp %-3s classmask %-6s parent %-5s  %s" % (
            r["ID"], r["ExpansionID"], r["ClassMask"], r["ParentTransmogSetID"], r["Name_lang"]))
    items = db.table("ItemSparse", ("ID", "Display_lang", "ExpansionID", "OverallQualityID", "InventoryType"),
                     where=lambda r: bool(rx.search(r["Display_lang"])))
    log("items matching %r: %d" % (text, len(items)))
    for r in items[:limit]:
        log("  item %7s exp %-3s q%s inv %-3s %s" % (r["ID"], r["ExpansionID"], r["OverallQualityID"],
                                                    r["InventoryType"], r["Display_lang"]))
    log("next: python bp.py select --work <W> --set <id> | --item <id> | --display <id> | --expansion <id>")
    return 0


def select(prj, log, a) -> int:
    t0 = time.time()
    db = Db2(prj)
    want_sets = {str(int(x)) for x in a.set}
    tsets_all = {r["ID"]: r for r in db.table("TransmogSet", ("ID", "Name_lang", "ClassMask", "ExpansionID",
                                                               "ParentTransmogSetID", "TransmogSetGroupID",
                                                               "ItemNameDescriptionID"))}
    if a.family and want_sets:
        roots = {tsets_all[s]["ParentTransmogSetID"] if tsets_all.get(s, {}).get("ParentTransmogSetID", "0") not in ("", "0") else s
                 for s in want_sets}
        want_sets |= {sid for sid, r in tsets_all.items()
                      if sid in roots or r["ParentTransmogSetID"] in roots}
    weapons_of_exp = set()
    for e in a.expansion:
        if not getattr(a, "weapons_only", False):
            want_sets |= {sid for sid, r in tsets_all.items() if r["ExpansionID"] == str(int(e))
                          and not r["Name_lang"].lstrip().startswith("[")}      # [DNT]/[PH] internal sets
        if getattr(a, "sets_only", False):
            continue
        for r in db.table("ItemSparse", ("ID", "ExpansionID", "OverallQualityID"),
                          where=lambda r, e=e: r["ExpansionID"] == str(int(e))):
            if int(r["OverallQualityID"] or 0) >= a.min_quality:
                weapons_of_exp.add(r["ID"])
    unknown = [s for s in want_sets if s not in tsets_all]
    if unknown:
        log("FAIL: no TransmogSet", unknown)
        return 2
    set_ima = collections.defaultdict(list)
    for r in db.table("TransmogSetItem", ("TransmogSetID", "ItemModifiedAppearanceID")):
        if r["TransmogSetID"] in want_sets:
            set_ima[r["TransmogSetID"]].append(r["ItemModifiedAppearanceID"])
    want_items = {str(int(x)) for x in a.item}
    want_apps = {str(int(x)) for x in a.appearance}
    want_disp = {str(int(x)) for x in a.display}
    ima_ids = {x for v in set_ima.values() for x in v}
    ima = {}
    for r in db.table("ItemModifiedAppearance", ("ID", "ItemID", "ItemAppearanceID", "ItemAppearanceModifierID")):
        if r["ID"] in ima_ids or r["ItemID"] in want_items or r["ItemAppearanceID"] in want_apps:
            ima[r["ID"]] = r
    item_cols = ("ID", "ClassID", "SubclassID", "InventoryType", "SheatheType", "Material", "IconFileDataID")
    items_all = {}
    if weapons_of_exp:
        # an expansion's weapons, shields and held off-hands (armour comes with its sets)
        for r in db.table("Item", item_cols, where=lambda r: r["ID"] in weapons_of_exp):
            if r["ClassID"] == "2" or (r["ClassID"] == "4" and r["SubclassID"] == "6") or \
                    r["InventoryType"] in ("13", "14", "15", "17", "21", "22", "23", "25", "26"):
                items_all[r["ID"]] = r
        for r in db.table("ItemModifiedAppearance", ("ID", "ItemID", "ItemAppearanceID", "ItemAppearanceModifierID")):
            if r["ItemID"] in items_all:
                ima[r["ID"]] = r
    app_ids = {r["ItemAppearanceID"] for r in ima.values()} | want_apps
    app = {r["ID"]: r for r in db.table("ItemAppearance", ("ID", "DisplayType", "ItemDisplayInfoID",
                                                          "DefaultIconFileDataID"))
           if r["ID"] in app_ids or r["ItemDisplayInfoID"] in want_disp}
    item_ids = {r["ItemID"] for r in ima.values()}
    items_all.update({r["ID"]: r for r in db.table("Item", item_cols, where=lambda r: r["ID"] in item_ids)})
    sparse = {r["ID"]: r for r in db.table("ItemSparse", ("ID", "Display_lang", "OverallQualityID",
                                                          "AllowableClass", "ItemNameDescriptionID", "ExpansionID"),
                                           where=lambda r: r["ID"] in item_ids)}
    nd = {r["ID"]: r["Description_lang"] for r in db.table("ItemNameDescription", ("ID", "Description_lang"))}

    displays = {}

    def disp(d, slot, icon):
        e = displays.get(d)
        if e is None:
            e = displays[d] = {"slot": slot, "sets": [], "items": [], "icon": int(icon or 0)}
        return e

    def item_rec(iid, mod):
        it = items_all.get(iid, {})
        sp = sparse.get(iid, {})
        return {"item": int(iid), "modifier": int(mod or 0), "name": sp.get("Display_lang", ""),
                "label": nd.get(sp.get("ItemNameDescriptionID", ""), ""),
                "inv": int(it.get("InventoryType") or 0), "class": int(it.get("ClassID") or 0),
                "sub": int(it.get("SubclassID") or 0), "sheathe": int(it.get("SheatheType") or 0),
                "material": int(it.get("Material") or 0), "quality": int(sp.get("OverallQualityID") or 0),
                "icon": int(it.get("IconFileDataID") or 0)}

    sets_out = {}
    for sid in sorted(want_sets, key=int):
        s = tsets_all[sid]
        slots = collections.defaultdict(list)
        for mid in set_ima.get(sid, []):
            im = ima.get(mid)
            ap = app.get(im["ItemAppearanceID"]) if im else None
            if not ap or ap["ItemDisplayInfoID"] in ("", "0"):
                continue
            slot = SLOT.get(int(ap["DisplayType"]), "type%s" % ap["DisplayType"])
            e = disp(ap["ItemDisplayInfoID"], slot, ap["DefaultIconFileDataID"])
            if int(sid) not in e["sets"]:
                e["sets"].append(int(sid))
            e["items"].append(item_rec(im["ItemID"], im["ItemAppearanceModifierID"]))
            slots[slot].append(int(ap["ItemDisplayInfoID"]))
        sets_out[sid] = {"name": s["Name_lang"], "label": nd.get(s["ItemNameDescriptionID"], ""),
                         "classmask": int(s["ClassMask"] or 0), "expansion": int(s["ExpansionID"] or 0),
                         "parent": int(s["ParentTransmogSetID"] or 0),
                         "slots": {k: sorted(set(v)) for k, v in slots.items()}}
    for im in ima.values():
        if im["ItemID"] in want_items or im["ItemAppearanceID"] in want_apps or im["ItemID"] in items_all:
            ap = app.get(im["ItemAppearanceID"])
            if ap and ap["ItemDisplayInfoID"] not in ("", "0"):
                e = disp(ap["ItemDisplayInfoID"], SLOT.get(int(ap["DisplayType"]), "type%s" % ap["DisplayType"]),
                         ap["DefaultIconFileDataID"])
                if all(x["item"] != int(im["ItemID"]) for x in e["items"]):
                    e["items"].append(item_rec(im["ItemID"], im["ItemAppearanceModifierID"]))
    for ap in app.values():
        if ap["ItemDisplayInfoID"] in want_disp:
            disp(ap["ItemDisplayInfoID"], SLOT.get(int(ap["DisplayType"]), "type%s" % ap["DisplayType"]),
                 ap["DefaultIconFileDataID"])
    for d in want_disp - set(displays):
        log("WARN display %s has no ItemAppearance: slot unknown, it will be skipped" % d)
    for e in displays.values():
        if not e["icon"] and e["items"]:
            e["icon"] = e["items"][0]["icon"]
    # filters that split a big port into archives (an archive stays < 2 GB)
    if getattr(a, "slots", None):
        keep = {x.strip() for x in a.slots.split(",") if x.strip()}
        displays = {d: e for d, e in displays.items() if e["slot"] in keep}
    if getattr(a, "armor", None):
        sub = {"cloth": 1, "leather": 2, "mail": 3, "plate": 4}[a.armor.strip().lower()]
        displays = {d: e for d, e in displays.items()
                    if e["slot"] in WEAPON_SLOTS or any(i["class"] == 4 and i["sub"] == sub for i in e["items"])}
    for s in sets_out.values():
        s["slots"] = {k: [x for x in v if str(x) in displays] for k, v in s["slots"].items()}
        s["slots"] = {k: v for k, v in s["slots"].items() if v}
    sets_out = {k: v for k, v in sets_out.items() if v["slots"]}
    attach_files(db, displays)
    sel = {"build": prj.build, "sets": sets_out, "displays": displays}
    if a.add:
        old = bpcore.load_json(prj.path("selection.json"), {"sets": {}, "displays": {}})
        old["sets"].update(sets_out)
        for d, e in displays.items():
            old["displays"].setdefault(d, e)
        sel = dict(old, build=prj.build)
    bpcore.save_json(prj.path("selection.json"), sel)
    by_slot = collections.Counter(e["slot"] for e in sel["displays"].values())
    missing = [d for d, e in sel["displays"].items() if e.get("missing_row")]
    log("selection: %d sets, %d displays %s (%.0fs)" % (len(sel["sets"]), len(sel["displays"]),
                                                        dict(by_slot), time.time() - t0))
    if missing:
        log("WARN %d displays have no ItemDisplayInfo row at this build: %s" % (len(missing), missing[:20]))
    log("next: python bp.py fetch --work <W>")
    return 0


def attach_files(db, displays) -> None:
    """Every file each display names, for every retail race (resolve.py doc)."""
    want = set(displays)
    idi = {r["ID"]: r for r in db.table("ItemDisplayInfo", where=lambda r: r["ID"] in want)}
    res, mat = set(), set()
    for r in idi.values():
        for k in (0, 1):
            if r["ModelResourcesID_%d" % k] not in ("", "0"):
                res.add(r["ModelResourcesID_%d" % k])
            if r["ModelMaterialResourcesID_%d" % k] not in ("", "0"):
                mat.add(r["ModelMaterialResourcesID_%d" % k])
    modelmat = collections.defaultdict(list)
    for r in db.table("ItemDisplayInfoModelMatRes", where=lambda r: r["ItemDisplayInfoID"] in want):
        modelmat[r["ItemDisplayInfoID"]].append(r)
        mat.add(r["MaterialResourcesID"])
    secmat = collections.defaultdict(list)
    for r in db.table("ItemDisplayInfoMaterialRes", where=lambda r: r["ItemDisplayInfoID"] in want):
        secmat[r["ItemDisplayInfoID"]].append(r)
        mat.add(r["MaterialResourcesID"])
    mfd = collections.defaultdict(list)
    for r in db.table("ModelFileData", ("FileDataID", "ModelResourcesID"), where=lambda r: r["ModelResourcesID"] in res):
        mfd[r["ModelResourcesID"]].append(r)
    tfd = collections.defaultdict(list)
    for r in db.table("TextureFileData", ("FileDataID", "MaterialResourcesID", "UsageType"),
                      where=lambda r: r["MaterialResourcesID"] in mat):
        tfd[r["MaterialResourcesID"]].append(r)
    mids = {r["FileDataID"] for v in mfd.values() for r in v}
    tids = {r["FileDataID"] for v in tfd.values() for r in v}
    cmfd = {r["ID"]: r for r in db.table("ComponentModelFileData", where=lambda r: r["ID"] in mids)}
    ctfd = {r["ID"]: r for r in db.table("ComponentTextureFileData", where=lambda r: r["ID"] in tids)}

    def rg(row):
        return (int(row["RaceID"]) if row else 0, int(row["GenderIndex"]) if row else -1,
                int(row["ClassID"]) if row else 0)
    for d, e in displays.items():
        r = idi.get(d)
        if not r:
            e["missing_row"] = True
            continue
        e["row"] = dict(r)
        e["models"] = []
        for k in (0, 1):
            for m in mfd.get(r["ModelResourcesID_%d" % k], []):
                c = cmfd.get(m["FileDataID"])
                race, g, cl = rg(c)
                e["models"].append({"k": k, "fdid": int(m["FileDataID"]), "race": race, "gender": g, "class": cl,
                                    "pos": int(c["PositionIndex"]) if c else -1})
        e["model_tex"] = []
        for k in (0, 1):
            for t in tfd.get(r["ModelMaterialResourcesID_%d" % k], []):
                race, g, cl = rg(ctfd.get(t["FileDataID"]))
                e["model_tex"].append({"k": k, "fdid": int(t["FileDataID"]), "usage": int(t["UsageType"]),
                                       "race": race, "gender": g, "class": cl})
        e["extra_tex"] = []
        for mm in modelmat.get(d, []):
            for t in tfd.get(mm["MaterialResourcesID"], []):
                race, g, cl = rg(ctfd.get(t["FileDataID"]))
                e["extra_tex"].append({"type": int(mm["TextureType"]), "model": int(mm["ModelIndex"]),
                                       "fdid": int(t["FileDataID"]), "usage": int(t["UsageType"]),
                                       "race": race, "gender": g, "class": cl})
        e["sections"] = []
        for sm in secmat.get(d, []):
            for t in tfd.get(sm["MaterialResourcesID"], []):
                race, g, cl = rg(ctfd.get(t["FileDataID"]))
                e["sections"].append({"section": int(sm["ComponentSection"]), "fdid": int(t["FileDataID"]),
                                      "race": race, "gender": g, "class": cl})


_CHR = {}


def fallback_of(prj, race: int, sex: int):
    """Retail ChrRaces Male/FemaleModelFallbackRaceID/Sex, or None."""
    if prj.build not in _CHR:
        cols = ("ID", "MaleModelFallbackRaceID", "MaleModelFallbackSex",
                "FemaleModelFallbackRaceID", "FemaleModelFallbackSex")
        _CHR[prj.build] = {int(r["ID"]): r for r in Db2(prj).table("ChrRaces", cols)}
    r = _CHR[prj.build].get(race)
    if not r:
        return None
    col = "MaleModelFallback" if sex == 0 else "FemaleModelFallback"
    fr, fs = int(r[col + "RaceID"] or 0), int(r[col + "Sex"] or 0)
    return (fr, fs) if fr else None


def needed_races(prj) -> set:
    """Retail race ids whose per-race files the client's bodies wear (with
    their fallback chains), plus 0 (race-agnostic files)."""
    sc = SC.load(prj)
    out = {0}
    for r in sc["races"]:
        if r.get("retail") is None:
            continue
        for sex in (0, 1):
            seen, cur = set(), (r["retail"], sex)
            while cur and cur not in seen:
                seen.add(cur)
                out.add(cur[0])
                cur = fallback_of(prj, *cur)
    return out


def fetch(prj, log) -> int:
    sel = bpcore.load_json(prj.path("selection.json"))
    races = needed_races(prj)
    F = Files(prj)
    first = set()
    for e in sel["displays"].values():
        for m in e.get("models", []):
            if m["race"] in races:
                first.add(m["fdid"])
        for key in ("model_tex", "extra_tex", "sections"):
            for t in e.get(key, []):
                if t["race"] in races:
                    first.add(t["fdid"])
        if e.get("icon"):
            first.add(e["icon"])
    log("stage 1: %d files the rows name (races %s)" % (len(first), sorted(races)))
    res = F.fetch(first, quiet=False)
    bad = {k: v for k, v in res.items() if v.startswith("FAILED")}
    # stage 2: what every model names, recursively
    todo = sorted({m["fdid"] for e in sel["displays"].values() for m in e.get("models", []) if m["race"] in races})
    seen, need, enc, gone = set(), set(), [], []
    while todo:
        f = todo.pop()
        if f in seen:
            continue
        seen.add(f)
        try:
            data = F.read(f)
        except Encrypted:
            enc.append(f)
            continue
        except (Missing, IOError):
            gone.append(f)
            continue
        if data[:4] not in (b"MD21", b"MD20", b"SKL1"):
            continue
        d = RM.deps(data)
        for kind, ids in d.items():
            if kind == "skin":
                ids = ids[:1]                 # the client reads LOD0 (00.skin) only
            for i in ids:
                need.add(int(i))
                if kind in ("recursive", "geometry_particle", "skel"):
                    todo.append(int(i))
    log("stage 2: %d models read, %d dependencies" % (len(seen), len(need)))
    res2 = F.fetch(need - set(res), quiet=False)
    bad.update({k: v for k, v in res2.items() if v.startswith("FAILED")})
    for k, v in list(res.items()) + list(res2.items()):
        if v == "missing":
            gone.append(k)
    for f in sorted(need | first):
        if F.have(f) and not F.try_read(f):
            enc.append(f)
    rep = {"failed": {str(k): v for k, v in bad.items()}, "missing": sorted(set(gone)),
           "encrypted": sorted(set(enc))}
    bpcore.save_json(prj.path("reports", "fetch.json"), rep)
    log("fetch: %d failed, %d missing at this build, %d encrypted (unreleased; those displays will not ship)"
        % (len(bad), len(rep["missing"]), len(rep["encrypted"])))
    if bad:
        log("FAILED downloads - run fetch again (it resumes):", list(bad.items())[:10])
        return 1
    log("next: python bp.py newtech --work <W>")
    return 0
