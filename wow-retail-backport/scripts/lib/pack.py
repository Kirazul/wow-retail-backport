r"""
pack.py - `bp.py pack`: THE RESULT, in <work>/out/.

  out/<patch>.MPQ            every file the rows reach (gates.closure) + the
                             three extended tables under DBFilesClient\
  out/dbc/*.dbc              the same tables, for the SERVER's dbc folder
  out/sql/item_template.sql  the server's items
  out/INSTALL.txt            the install steps for a player and a server admin

Gates (any failure: exit 1, the previous archive is left untouched):
  PRIVATE   every art path lives under \<namespace>\ or is an icon named
            <icon_prefix>_*: nothing we ship can replace a file of the
            client or of another patch. The only shared names are the three
            DBFilesClient tables, which are the client's own winning copies
            plus appended rows (scan.py read them from the client).
  AUDIT     reports/audit.json exists and has no FAIL (run audit first)
  READBACK  every file read back from the written archive equals its input
  SIZE      < 2 GB per archive (the 3.3.5 MPQ reader is 32-bit): art beyond 1.5 GB
            goes to more archives on spare letters; the tables stay in the top one
"""
from __future__ import annotations

import os

import bpcore
import gates
import scan as SC
from mpq_read import MpqReader
from mpq_write import MpqWriter

LIMIT = 2 * 1024 ** 3 - 1
PART_BYTES = 1536 * 1024 ** 2      # uncompressed art per archive (compressed it is always smaller)


def run(prj, log) -> int:
    sc = SC.load(prj)
    name = prj.cfg.get("patch_name") or sc.get("patch_name")
    if not name:
        log("FAIL:", sc.get("patch_problem", "no archive name"))
        return 1
    audit = bpcore.load_json(prj.path("reports", "audit.json"), None)
    if audit is None:
        log("FAIL: run  python bp.py audit  first")
        return 1
    if audit.get("fails"):
        log("FAIL: the last audit has problems - fix them, rebuild, audit again")
        return 1
    plan = bpcore.load_json(prj.path("build", "plan.json"))
    want, _man = gates.closure(prj, plan, sc)
    ns = prj.ns.lower()
    icon = (prj.cfg.get("icon_prefix") or "bp").lower() + "_"
    bad = [p for low, (p, _w) in want.items()
           if ("\\%s\\" % ns) not in low and not low.startswith("interface\\icons\\" + icon)]
    if bad:
        log("FAIL: not private (would replace someone's file):", bad[:10])
        return 1
    files = [(p, os.path.join(prj.build_dir, p)) for _low, (p, _w) in sorted(want.items())]
    for t in ("ItemDisplayInfo", "Item", "HelmetGeosetVisData"):
        files.append(("DBFilesClient\\%s.dbc" % t, prj.path("out", "dbc", t + ".dbc")))
    missing = [p for p, f in files if not os.path.isfile(f)]
    if missing:
        log("FAIL: missing:", missing[:10])
        return 1
    # one archive holds the tables (it must outrank every other: `name`); art
    # beyond PART_BYTES (uncompressed) goes to further archives on the spare
    # letters scan found - their paths are private, so their rank never matters
    tables = [x for x in files if x[0].startswith("DBFilesClient\\")]
    art = [x for x in files if not x[0].startswith("DBFilesClient\\")]
    parts, cur, cur_bytes = [], list(tables), 0
    for p, f in art:
        n = os.path.getsize(f)
        if cur_bytes + n > PART_BYTES and cur_bytes:
            parts.append(cur)
            cur, cur_bytes = [], 0
        cur.append((p, f))
        cur_bytes += n
    parts.append(cur)
    spare = [x for x in sc.get("spare_letters", [])]
    names = [name]
    for _i in range(1, len(parts)):
        if not spare:
            log("FAIL: %d archives needed but the client has no free patch letters left" % len(parts))
            return 1
        names.append("patch-%s.MPQ" % spare.pop(0).upper())   # lowest free letters first
    for old in os.listdir(prj.path("out", "x")[:-2]):
        if old.lower().startswith("patch-") and old.lower().endswith(".mpq") and old not in names:
            os.remove(prj.path("out", old))          # a previous run's extra part
    total_files = 0
    for pname, part in zip(names, parts):
        out = prj.path("out", pname)
        w = MpqWriter(out)
        total = 0
        for p, f in part:
            data = open(f, "rb").read()
            total += len(data)
            w.add(p, data)
        w.close()
        size = os.path.getsize(out)
        if size > LIMIT:
            log("FAIL: %s is %.2f GB (> 2 GB)" % (pname, size / 1024 ** 3))
            return 1
        r = MpqReader(out)
        differ = [p for p, f in part if r.read(p) != open(f, "rb").read()]
        r.fh.close()
        if differ:
            log("FAIL: %d files differ after read-back in %s: %s" % (len(differ), pname, differ[:5]))
            return 1
        total_files += len(part)
        log("pack: %s  %d files, %.1f MB (from %.1f MB), read-back identical%s" % (
            out, len(part), size / 1e6, total / 1e6, "  [tables]" if part is parts[0] else ""))
    write_install(prj, names, sc)
    ext = prj.path("out", "extension", "extension.json")
    if os.path.isfile(ext):
        if pack_extension(prj, log, name, bpcore.load_json(ext)):
            return 1
    log("next: python bp.py sheet --work <W>   (look at every picture), then install (out/INSTALL.txt)")
    return 0


def pack_extension(prj, log, name, ext) -> int:
    """Tier 2 (only for a client mod that binds them): every exported piece,
    its skin, type-0 textures and .anim files, in out/extension/<name>-ext.MPQ."""
    import struct
    import m2model as M
    import m2_port as MP
    root = prj.build_dir
    want = {}
    for d in ext.values():
        for p in d["pieces"]:
            if "model" not in p:
                continue
            rel = p["model"]
            want[rel.lower()] = rel
            want[(rel[:-3] + "00.skin").lower()] = rel[:-3] + "00.skin"
            body = open(os.path.join(root, rel), "rb").read()
            n, o = struct.unpack_from("<II", body, MP.H_TEXTURES)
            for i in range(n):
                typ, _fl, ln, no = struct.unpack_from("<IIII", body, o + i * 16)
                if typ == 0 and ln > 1:
                    t = body[no:no + ln].split(b"\0")[0].decode("latin1")
                    want[t.lower()] = t
            for _s, a, v in M.external_set(body):
                if a <= 505:
                    t = M.anim_name(rel, a, v)
                    want[t.lower()] = t
    missing = [p for p in want.values() if not os.path.isfile(os.path.join(root, p))]
    if missing:
        log("FAIL extension: missing", missing[:10])
        return 1
    out = prj.path("out", "extension", name[:-4] + "-ext.MPQ")
    w = MpqWriter(out)
    for p in sorted(want.values()):
        w.add(p, open(os.path.join(root, p), "rb").read())
    w.close()
    r = MpqReader(out)
    bad = [p for p in want.values() if r.read(p) != open(os.path.join(root, p), "rb").read()]
    r.fh.close()
    if bad:
        log("FAIL extension read-back:", bad[:10])
        return 1
    log("pack: %s  %d files (Tier 2: only for a client mod - see references/client-extension.md)"
        % (out, len(want)))
    return 0


def write_install(prj, names, sc) -> None:
    name = " + ".join(names)
    txt = r"""INSTALL - retail backport for a 3.3.5a (12340) client
Built from retail build {build} for the client at {data}.

PLAYER (every player of the server needs this)
 1. Close the game.
 2. Copy {name} into the game's Data folder (<WoW>\Data\)
    (keep the names: the client loads patch archives by name; {first} holds
     the tables and outranks every patch the client had when it was scanned)
 3. Delete the game's Cache folder (<WoW>\Cache) so item data is fetched again.
 4. Start the game.
 To remove: delete those archives from <WoW>\Data\ and the Cache folder.

SERVER ADMIN (AzerothCore / TrinityCore 3.3.5)
 1. Stop the worldserver.
 2. Back up your dbc folder, then copy dbc\ItemDisplayInfo.dbc, dbc\Item.dbc and
    dbc\HelmetGeosetVisData.dbc into the server's dbc folder (DataDir\dbc).
 3. Back up item_template, then run sql\item_template.sql on the world database.
 4. Start the worldserver. Test: .additem <id> (ids in the SQL file).

IF THE SERVER'S CLIENT PATCHES CHANGE (a new patch with DBFilesClient tables):
 rebuild: python bp.py scan, tables, audit, pack - the tables are the client's
 own plus our rows, so they must be re-made from the new winning copies.
""".format(build=prj.build, data=sc["data_dir"], name=name, first=names[0])
    with open(prj.path("out", "INSTALL.txt"), "w", encoding="utf-8") as f:
        f.write(txt)
