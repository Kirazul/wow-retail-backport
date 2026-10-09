"""
scan.py - learn what the TARGET client is, from its own files (never assumed).

Writes <work>/client/scan.json and copies the client's WINNING copy of every
table the backport extends to <work>/client/dbc/. Works on any 3.3.5a client:
stock, HD-patched, or a custom realm client with extra races, because every
fact is read from that client:

  archives   mount order (wow.exe's own rule, client.py) and what is NOT mounted
  races      every ChrRaces row: id, ClientPrefix (helm file suffix), file
             string (body folder), whether male/female body models exist, and
             which RETAIL race's item cuts it wears (same id when the prefix
             matches retail's; else the retail race with the same prefix; else
             bp.json "race_map"; else none -> its helms are invisible)
  bodies     per race/sex: path, vertex/bone counts, geosets, helm point (11)
  ids        the highest id in ItemDisplayInfo / Item / HelmetGeosetVisData
  patch      the archive name our output must use to win the tables it extends

The ChrRaces columns are FOUND (row 1 holds "Hu" / "Human", row 2 "Or" /
"Orc"), not taken from a layout table, so a custom ChrRaces works too.
"""
from __future__ import annotations

import os
import re
import string

import bpcore
import retail_m2 as RM
from client import Client
from wdbc import Wdbc

TABLES = ("ChrRaces", "Item", "ItemDisplayInfo", "HelmetGeosetVisData", "ItemSubClass", "ItemClass")
SEX = ("Male", "Female")


def _find_col(t: Wdbc, want: dict) -> int:
    """The column (not 0) whose string is want[row id] for every given row."""
    rows = t.by_id()
    for c in range(1, t.fields):
        ok = True
        for rid, text in want.items():
            r = rows.get(rid)
            if not r or t.string(r[c]).lower() != text.lower():
                ok = False
                break
        if ok:
            return c
    raise ValueError("ChrRaces: no column holds %s - not a 3.3.5 ChrRaces layout" % want)


def body_info(data: bytes, skin: bytes | None) -> dict:
    md = RM.md20(data)
    att = {a: (b, p) for a, b, p in RM.attachments(md)}
    out = {"verts": len(RM.vertices(md)), "bones": len(RM.bones(md))}
    if 11 in att:
        out["helm_point"] = [round(x, 5) for x in att[11][1]]
        out["helm_bone"] = att[11][0]
    if skin:
        out["geosets"] = sorted({s["id"] for s in RM.skin(skin)["subs"]})
    return out


def scan(prj, log) -> dict:
    cfg = prj.cfg
    c = Client(cfg["client_data"], cfg.get("locale") or None, bool(cfg.get("wildcard_patches")))
    log("client:", cfg["client_data"], "locale", c.locale)
    log("mounted (highest first):", ", ".join(os.path.basename(p) for p in c.order))
    if c.skipped:
        log("NOT mounted (the client ignores them too):", ", ".join(os.path.basename(p) for p in c.skipped))
    for e in c.errors:
        log("UNREADABLE ARCHIVE:", e)
    tables = {}
    dbc = {}
    for n in TABLES:
        got = c.who("DBFilesClient\\%s.dbc" % n, data=True)
        if not got:
            raise SystemExit("the client has no DBFilesClient\\%s.dbc - is this a 3.3.5a Data folder?" % n)
        with open(prj.path("client", "dbc", n + ".dbc"), "wb") as f:
            f.write(got[1])
        dbc[n] = Wdbc(got[1])
        tables[n] = {"archive": os.path.basename(got[0]), "rows": len(dbc[n].rows),
                     "fields": dbc[n].fields, "max_id": dbc[n].max_id()}
        log("  %-20s from %-22s rows %6d fields %3d max id %d" % (
            n, tables[n]["archive"], tables[n]["rows"], tables[n]["fields"], tables[n]["max_id"]))
    if tables["ItemDisplayInfo"]["fields"] != 25 or tables["Item"]["fields"] != 8 \
            or tables["HelmetGeosetVisData"]["fields"] != 8:
        raise SystemExit("unexpected table layout (ItemDisplayInfo 25 / Item 8 / HelmetGeosetVisData 8 "
                         "fields expected): STOP - this client is not a 3.3.5a 12340 layout")
    cr = dbc["ChrRaces"]
    pcol = _find_col(cr, {1: "Hu", 2: "Or"})
    fcol = _find_col(cr, {1: "Human", 2: "Orc"})
    # retail races (same build as everything else)
    from wago import Db2
    retail = {int(r["ID"]): r for r in Db2(prj).table("ChrRaces", ("ID", "ClientPrefix", "ClientFileString"))}
    by_prefix = {}
    for rid, r in sorted(retail.items()):
        by_prefix.setdefault(r["ClientPrefix"].lower(), rid)
    race_map = {int(k): int(v) for k, v in (cfg.get("race_map") or {}).items()}
    races = []
    for r in sorted(cr.rows, key=lambda x: x[0]):
        rid, pre, fs = r[0], cr.string(r[pcol]), cr.string(r[fcol])
        # ChrRaces.Flags (column 1) bit 0x1 = NOT playable (measured on 12340:
        # the 11 playable races clear it, every NPC race sets it)
        ent = {"id": rid, "prefix": pre, "file": fs, "bodies": {}, "playable": not (r[1] & 1)}
        if rid in race_map and race_map[rid] == 0:
            ent["retail"], ent["how"] = None, "bp.json race_map 0: accepted - no retail items (helms invisible)"
        elif rid in race_map:
            ent["retail"], ent["how"] = race_map[rid], "bp.json race_map"
        elif rid in retail and retail[rid]["ClientPrefix"].lower() == pre.lower():
            ent["retail"], ent["how"] = rid, "same id and prefix as retail"
        elif pre.lower() in by_prefix:
            ent["retail"], ent["how"] = by_prefix[pre.lower()], "retail race with the same prefix"
        else:
            ent["retail"], ent["how"] = None, "NO retail match: add it to bp.json race_map or its helms stay invisible"
        for sx in SEX:
            p = "Character\\%s\\%s\\%s%s.m2" % (fs, sx, fs, sx)
            d = c.read(p) if fs else None
            if d and d[:4] == b"MD20":
                sk = c.read(p[:-3] + "00.skin")
                info = body_info(d, sk)
                info["path"] = p
                info["archive"] = os.path.basename(c.who(p)[0])
                ent["bodies"][sx[0]] = info
        races.append(ent)
        log("  race %3d %-4s %-18s %-8s bodies %-3s retail %-4s (%s)" % (
            rid, pre, fs, "playable" if ent["playable"] else "NPC", "".join(sorted(ent["bodies"])) or "-",
            ent["retail"], ent["how"]))
    # our archive must outrank every archive that ships a table we extend
    letters = []
    for p in c.order:
        m = re.fullmatch(r"patch-([a-z0-9])\.mpq", os.path.basename(p).lower())
        if m:
            letters.append(m.group(1))
    alphabet = string.digits + string.ascii_lowercase
    top = max((alphabet.index(x) for x in letters), default=-1)
    free = [x for x in alphabet[top + 1:]]
    patch = cfg.get("patch_name") or ("patch-%s.MPQ" % free[0].upper() if free else "")
    # every other free letter (for art-only archives when one archive is not enough)
    used = set(letters) | {os.path.basename(p).lower()[6:7] for p in c.skipped
                           if re.fullmatch(r"patch-[a-z0-9]\.mpq(\..*)?", os.path.basename(p).lower())}
    spare = [x for x in alphabet if x not in used and ("patch-%s.mpq" % x) != patch.lower()]
    out = {"data_dir": cfg["client_data"], "locale": c.locale, "wildcard_patches": c.wildcard,
           "spare_letters": spare,
           "mounted": [os.path.basename(p) for p in c.order],
           "not_mounted": [os.path.basename(p) for p in c.skipped], "unreadable": c.errors,
           "tables": tables, "races": races, "patch_name": patch,
           "chrraces_columns": {"prefix": pcol, "file": fcol}}
    if not patch:
        out["patch_problem"] = ("patch-Z.MPQ is taken: no single-letter name outranks it. Set bp.json "
                                "patch_name, or ship the tables inside the highest patch yourself")
        log("PROBLEM:", out["patch_problem"])
    else:
        log("output archive:", patch, "(outranks every mounted patch: tables we extend will win)")
    bpcore.save_json(prj.path("client", "scan.json"), out)
    bad = [r for r in races if r["playable"] and r.get("retail") is None and r["id"] not in race_map]
    for r in bad:
        log("FAIL: playable race %d (%s) has no retail match - set bp.json race_map {\"%d\": <retail race id>} "
            "(or 0 to accept invisible helms for it), then scan again" % (r["id"], r["prefix"], r["id"]))
    if c.errors:
        log("FAIL: unreadable archives (the client cannot read them either?): %s" % c.errors)
    if bad or c.errors or not patch:
        return 1
    log("scan: PASS")
    return 0


def load(prj) -> dict:
    p = prj.path("client", "scan.json")
    if not os.path.isfile(p):
        raise SystemExit("no client scan: run  python bp.py scan")
    return bpcore.load_json(p)
