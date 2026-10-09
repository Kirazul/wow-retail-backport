r"""
tables.py - the rows a 3.3.5a client and server need, appended to the
client's OWN tables (never replacing a row), plus the server SQL.

  out/dbc/ItemDisplayInfo.dbc        client table + one row per built display
  out/dbc/Item.dbc                   client table + one row per new item
  out/dbc/HelmetGeosetVisData.dbc    client table + one row per retail helm-vis id used
  out/sql/item_template.sql          the server's items (AzerothCore or TrinityCore)
  ids.json                           retail display -> new display id / item id
                                     (stable: a re-run keeps every id it gave)

These DBCs go in TWO places: the patch MPQ (DBFilesClient\, pack.py does it)
and the server's dbc folder (the server validates item displays against
ItemDisplayInfo.dbc and reads Item.dbc).

ROW RULES (each measured; references/rules.md)
  ItemDisplayInfo (25 fields): 0 id, 1-2 ModelName (L/R), 3-4 ModelTexture,
    5-6 InventoryIcon, 7-9 GeosetGroup[3] (remapped to this client's body
    variants), 10 Flags = 0 (retail bits mean other things), 11 SpellVisualID 0,
    12 GroupSoundIndex = the client's most common value for items of the same
    class/subclass/inventory type, 13-14 HelmetGeosetVis (male, female),
    15-22 the 8 body region textures, 23 ItemVisual 0 (retail ids name rows this
    client does not have: reported), 24 ParticleColorID 0 (baked into the model).
  Item (8 fields): id, class, subclass, sound override -1, material,
    display id, inventory type, sheathe type (retail -> 3.3.5 map).
  HelmetGeosetVisData (8): id + 7 race bitmasks (bit = 1 << client race id) for
    retail HideGeosetGroup 0, 1, 2, 3, 7, 16, 17 (hair, facial 1-3, ears, misc).
"""
from __future__ import annotations

import collections
import os

import bpcore
import scan as SC
from wago import Db2
from wdbc import Wdbc

# retail subclass -> 3.3.5 subclass where the NAMES differ (all others: same
# number, same name - measured on ItemSubClass of both games)
SUBCLASS_FALLBACK = {(2, 9): (2, 7, "Warglaives -> One-Handed Swords (one-handed, held and swung alike)"),
                     (2, 11): (2, 13, "Bear Claws -> Fist Weapons"),
                     (2, 12): (2, 13, "Cat Claws -> Fist Weapons"),
                     (4, 5): (4, 0, "Cosmetic -> Miscellaneous"),
                     (4, 11): (4, 0, "Relic -> Miscellaneous")}
HIDE_COL = {0: 1, 1: 2, 2: 3, 3: 4, 7: 5, 16: 6, 17: 7}


def _dbc(prj, name) -> Wdbc:
    p = prj.path("client", "dbc", name + ".dbc")
    if not os.path.isfile(p):
        raise SystemExit("no %s: run  python bp.py scan" % p)
    return Wdbc(open(p, "rb").read())


def subclass_map(prj) -> dict:
    cl = _dbc(prj, "ItemSubClass")
    name_col = 10
    client = {(r[0], r[1]): cl.string(r[name_col]) for r in cl.rows}
    out = {}
    for r in Db2(prj).table("ItemSubClass", ("ClassID", "SubClassID", "DisplayName_lang")):
        key = (int(r["ClassID"]), int(r["SubClassID"]))
        if client.get(key, "").lower() == r["DisplayName_lang"].lower():
            out[key] = (key[0], key[1], "same")
        elif key in SUBCLASS_FALLBACK:
            out[key] = SUBCLASS_FALLBACK[key]
    return out


def run(prj, log) -> int:
    plan = bpcore.load_json(prj.path("build", "plan.json"))
    sc = SC.load(prj)
    cfg = prj.cfg
    idi, item, hv = _dbc(prj, "ItemDisplayInfo"), _dbc(prj, "Item"), _dbc(prj, "HelmetGeosetVisData")
    ids = bpcore.load_json(prj.path("ids.json"), {"display": {}, "item": {}, "helmvis": {}})
    d0 = int(cfg.get("display_id_start") or 0) or ((idi.max_id() // 1000) + 1) * 1000
    i0 = int(cfg.get("item_id_start") or 0) or 900000
    h0 = int(cfg.get("helmvis_id_start") or 0) or hv.max_id() + 1
    taken_d, taken_i, taken_h = idi.ids(), item.ids(), hv.ids()

    def alloc(kind, key, start, taken):
        m = ids[kind]
        if key in m:
            return m[key]
        used = set(m.values()) | taken
        n = max([start - 1] + [v for v in m.values()]) + 1
        while n in used:
            n += 1
        m[key] = n
        return n
    # client conventions measured from its own rows
    sound = collections.defaultdict(collections.Counter)
    by = idi.by_id()
    for r in item.rows:
        d = by.get(r[5])
        if d:
            sound[(r[1], r[2], r[6])][d[12]] += 1
    smap = subclass_map(prj)
    # helm vis rows
    hgd = Db2(prj).table("HelmetGeosetData", ("HelmetGeosetVisDataID", "RaceID", "HideGeosetGroup"))
    client_of_retail = collections.defaultdict(list)
    for r in sc["races"]:
        if r.get("retail") is not None:
            client_of_retail[r["retail"]].append(r["id"])
    vis_used = sorted({v for p in plan.values() if p.get("ok") for v in p["row"]["helmvis"] if v})
    vis_rows = collections.defaultdict(lambda: [0] * 8)
    for row in hgd:
        v = int(row["HelmetGeosetVisDataID"])
        if v not in vis_used:
            continue
        g = int(row["HideGeosetGroup"])
        col = HIDE_COL.get(g)
        if col is None:
            continue
        race = int(row["RaceID"])
        targets = [x["id"] for x in sc["races"]] if race == 0 else client_of_retail.get(race, [])
        for cid in targets:
            if cid < 32:
                vis_rows[v][col] |= 1 << cid
    new_vis = {}
    for v in vis_used:
        nid = alloc("helmvis", str(v), h0, taken_h)
        new_vis[v] = nid
        if nid not in taken_h:
            r8 = list(vis_rows[v])
            r8[0] = nid
            hv.add(r8)
    # names: one item per display; a name shared by several displays gets its label
    names = collections.Counter(p["items"][0]["name"] for p in plan.values() if p.get("ok") and p.get("items"))
    sql = []
    report = collections.Counter()
    for d, p in sorted(plan.items(), key=lambda x: int(x[0])):
        if not p.get("ok"):
            report["display not shipped: " + (p.get("error") or "?").split(":")[0]] += 1
            continue
        r = p["row"]
        did = alloc("display", d, d0, taken_d)
        iid = alloc("item", d, i0, taken_i)
        p["display_id"], p["item_id"] = did, iid
        cls, sub = p.get("class", 0), p.get("sub", 0)
        m = smap.get((cls, sub))
        if m is None:
            report["unknown retail subclass %d/%d -> 0/0 (Miscellaneous)" % (cls, sub)] += 1
            m = (4 if cls == 4 else cls, 0, "unknown")
        elif m[2] != "same":
            report["subclass " + m[2]] += 1
        cls335, sub335 = m[0], m[1]
        inv = p.get("inv", 0)
        snd = sound.get((cls335, sub335, inv))
        if did not in taken_d:
            vals = [0] * 25
            vals[0] = did
            for k in range(3):
                vals[7 + k] = int(r["gg335"][k])
            vals[12] = snd.most_common(1)[0][0] if snd else 0
            for k in range(2):
                vals[13 + k] = new_vis.get(r["helmvis"][k], 0)
            strings = {1: r["model"][0], 2: r["model"][1], 3: r["tex"][0], 4: r["tex"][1], 5: r["icon"]}
            for k in range(8):
                strings[15 + k] = r["regions"][k]
            idi.add(vals, strings)
            if r.get("itemvisual"):
                report["retail ItemVisual not carried (no 3.3.5 row)"] += 1
        if iid not in taken_i:
            item.add([iid, cls335, sub335, 0xFFFFFFFF, max(0, p.get("material", 0)), did, inv, p.get("sheath", 0)])
        it = p["items"][0] if p.get("items") else {}
        name = it.get("name") or ("Backport display %s" % d)
        if names[name] > 1 and it.get("label"):
            name += " (%s)" % it["label"]
        if names[name] > 1:
            name += " #%s" % d
        q = min(7, max(0, int(it.get("quality", 1))))
        sql.append((iid, cls335, sub335, name, did, q, inv, max(0, p.get("material", 0)), p.get("sheath", 0)))
    bpcore.save_json(prj.path("ids.json"), ids)
    bpcore.save_json(prj.path("build", "plan.json"), plan)
    for n, t in (("ItemDisplayInfo", idi), ("Item", item), ("HelmetGeosetVisData", hv)):
        with open(prj.path("out", "dbc", n + ".dbc"), "wb") as f:
            f.write(t.to_bytes())
    write_sql(prj, sql)
    log("tables: %d displays, %d items, %d helm-vis rows appended (client rows untouched)" %
        (len(sql), len(sql), len(new_vis)))
    log("  ids: displays from %d, items from %d (bp.json display_id_start / item_id_start)" % (d0, i0))
    for k, n in report.most_common():
        log("  %4d  %s" % (n, k))
    bpcore.save_json(prj.path("reports", "tables.json"), dict(report))
    log("next: python bp.py audit --work <W>")
    return 0


def write_sql(prj, rows) -> None:
    """item_template rows for AzerothCore / TrinityCore 3.3.5 (same column
    names for everything written here). Scoped: deletes only our entries."""
    def q(s):
        return "'" + s.replace("\\", "\\\\").replace("'", "''") + "'"
    lines = ["-- generated by wow-retail-backport (bp.py tables) for %s; build %s" % (prj.cfg.get("server"), prj.build),
             "-- items are cosmetic copies: any class, any race, level 1, no stats",
             "DELETE FROM `item_template` WHERE `entry` IN (%s);" % ",".join(str(r[0]) for r in rows) if rows else "",
             "INSERT INTO `item_template` (`entry`,`class`,`subclass`,`SoundOverrideSubclass`,`name`,`displayid`,"
             "`Quality`,`InventoryType`,`AllowableClass`,`AllowableRace`,`ItemLevel`,`RequiredLevel`,`Material`,"
             "`sheath`,`bonding`,`stackable`,`MaxDurability`) VALUES"]
    vals = []
    for iid, cls, sub, name, did, qual, inv, mat, sh in rows:
        vals.append("(%d,%d,%d,-1,%s,%d,%d,%d,-1,-1,1,0,%d,%d,0,1,0)" % (iid, cls, sub, q(name), did, qual, inv, mat, sh))
    lines.append(",\n".join(vals) + ";" if vals else "")
    with open(prj.path("out", "sql", "item_template.sql"), "w", encoding="utf-8") as f:
        f.write("\n".join(x for x in lines if x) + "\n")
