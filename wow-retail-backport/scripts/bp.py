r"""
bp.py - backport retail World of Warcraft items to a 3.3.5a (12340) client.

Every step is one command. Run them in this order (SKILL.md explains each):

  python bp.py init     --work W --client "<WoW>\Data" [--build B]
  python bp.py scan     --work W            what the client is (races, ids, tables)
  python bp.py newtech  --work W --learn    record what retail uses today (once per build)
  python bp.py find     --work W "text"     search retail items and sets by name
  python bp.py select   --work W --set 1234 --item 5678 --display 91011 --expansion 11 ...
  python bp.py fetch    --work W            download every retail file the selection needs
  python bp.py newtech  --work W            STOP if retail uses anything this kit does not know
  python bp.py measure  --work W            helm placement + body geosets for THIS client
  python bp.py build    --work W            convert everything (resumable)
  python bp.py tables   --work W            DBC rows + server SQL
  python bp.py audit    --work W            every gate; exit 1 on any problem
  python bp.py prove-gates --work W         plant 5 real defects in a copy: audit must catch all
  python bp.py pack     --work W            the patch MPQ + read-back + closure
  python bp.py sheet    --work W            pictures: look at every one
  python bp.py status   --work W            where the project stands

  python bp.py selftest                     the kit's own tests (no network, no client)
  python bp.py convert-model --work W FDID  any retail model -> a 3.3.5 model (doodads, effects...)

Exit code 0 = the step passed. Anything else: read the printed reason, then
SKILL.md "When a step fails". Never edit a file under build/ or out/ by hand.
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _toolpath  # noqa: E402,F401

import bpcore  # noqa: E402


def cmd_init(a) -> int:
    import wago
    work = os.path.abspath(a.work)
    p = os.path.join(work, "bp.json")
    cfg = dict(bpcore.DEFAULTS)
    if os.path.isfile(p):
        cfg.update(bpcore.load_json(p))
    if a.client:
        cfg["client_data"] = os.path.abspath(a.client)
    if a.build:
        cfg["build"] = a.build
    if not cfg["build"]:
        cfg["build"] = wago.latest_build()
        print("pinned the newest live retail build:", cfg["build"])
    for k in ("namespace", "server", "cache", "listfile", "locale"):
        v = getattr(a, k, None)
        if v:
            cfg[k] = v
    if not cfg["client_data"] or not os.path.isdir(cfg["client_data"]):
        print("FAIL: --client must be the client's Data folder (it holds common.MPQ)")
        return 2
    if not os.path.isfile(os.path.join(cfg["client_data"], "common.MPQ")) and \
            not any(f.lower() == "common.mpq" for f in os.listdir(cfg["client_data"])):
        print("FAIL: %s has no common.MPQ - not a 3.3.5a Data folder" % cfg["client_data"])
        return 2
    os.makedirs(work, exist_ok=True)
    bpcore.save_json(p, cfg)
    print("project:", work)
    print("settings:", p)
    for k in sorted(cfg):
        print("  %-18s %s" % (k, cfg[k]))
    print("next: python bp.py scan --work \"%s\"" % work)
    return 0


# THE STEP LEDGER (W/reports/steps.json): every pipeline step records PASS or
# FAIL; a step refuses to run until every step it depends on has PASSED, and
# re-running a step un-ticks everything that depends on it. Nothing can be
# skipped, nothing stale can ship.
REQUIRES = {"select": ["scan"], "fetch": ["select"], "newtech": ["fetch"], "measure": ["scan"],
            "build": ["scan", "select", "fetch", "newtech", "measure"], "tables": ["build"],
            "audit": ["tables"], "prove-gates": ["audit"], "pack": ["audit", "prove-gates"],
            "sheet": ["build"]}
ORDER = ["scan", "select", "fetch", "newtech", "measure", "build", "tables", "audit", "prove-gates",
         "pack", "sheet"]


def _dependents(step):
    out = set()
    todo = [step]
    while todo:
        s = todo.pop()
        for k, v in REQUIRES.items():
            if s in v and k not in out:
                out.add(k)
                todo.append(k)
    return out


def ledger(prj):
    return bpcore.load_json(prj.path("reports", "steps.json"), {})


def gate_step(prj, step) -> str | None:
    """None when `step` may run, else why not."""
    led = ledger(prj)
    missing = [r for r in REQUIRES.get(step, []) if not led.get(r, {}).get("ok")]
    if missing:
        return "step '%s' needs PASS first on: %s  (python bp.py status --work W)" % (step, ", ".join(missing))
    return None


def record_step(prj, step, ok) -> None:
    import time as _t
    led = ledger(prj)
    for d in _dependents(step):
        led.pop(d, None)
    led[step] = {"ok": bool(ok), "at": _t.strftime("%Y-%m-%d %H:%M:%S")}
    bpcore.save_json(prj.path("reports", "steps.json"), led)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def add(name, **kw):
        s = sub.add_parser(name, **kw)
        if name != "selftest":
            s.add_argument("--work", required=True, help="the project folder")
        return s

    s = add("init")
    s.add_argument("--client", help="the target client's Data folder")
    s.add_argument("--build", help="retail build to pin (default: newest live)")
    s.add_argument("--namespace")
    s.add_argument("--server", choices=["azerothcore", "trinitycore"])
    s.add_argument("--cache", help="an existing retail file cache folder to reuse")
    s.add_argument("--listfile", help="an existing community listfile to reuse")
    s.add_argument("--locale")
    add("scan")
    s = add("newtech")
    s.add_argument("--learn", action="store_true", help="record what retail uses at this build")
    s = add("find")
    s.add_argument("text")
    s.add_argument("--limit", type=int, default=40)
    s = add("select")
    s.add_argument("--set", action="append", default=[], help="TransmogSet id (repeatable)")
    s.add_argument("--family", action="store_true", help="with --set: every colourway of the set's family")
    s.add_argument("--item", action="append", default=[], help="retail Item id (repeatable)")
    s.add_argument("--appearance", action="append", default=[], help="ItemAppearance id")
    s.add_argument("--display", action="append", default=[], help="ItemDisplayInfo id")
    s.add_argument("--expansion", action="append", default=[], help="ExpansionID: every set + weapon of it")
    s.add_argument("--min-quality", type=int, default=0, help="with --expansion: skip items below this quality")
    s.add_argument("--sets-only", action="store_true", help="with --expansion: its transmog sets, no loose weapons")
    s.add_argument("--weapons-only", action="store_true", help="with --expansion: its weapons/shields/off-hands only")
    s.add_argument("--slots", help="keep only these slots, comma list: head,shoulder,chest,waist,legs,feet,"
                                   "wrist,hands,back,shirt,tabard,mainhand,offhand,ranged,held")
    s.add_argument("--armor", help="keep only this armour type: cloth,leather,mail,plate (weapons kept unless --slots)")
    s.add_argument("--add", action="store_true", help="add to the existing selection instead of replacing it")
    s = add("fetch")
    s = add("measure")
    s.add_argument("--only", choices=["helms", "geosets"])
    s = add("build")
    s.add_argument("--display", action="append", default=[])
    s.add_argument("--force", action="store_true")
    s.add_argument("--extension", action="store_true",
                   help="also export Tier-2 files (3D chest/leg/boot pieces, belt buckles) for a client mod")
    add("tables")
    s = add("audit")
    add("prove-gates")
    s = add("pack")
    s = add("sheet")
    s.add_argument("--display", action="append", default=[])
    add("status")
    s = add("phys")
    s.add_argument("fdid", type=int, nargs="?")
    s.add_argument("--settle", action="store_true", help="settle it at rest and report")
    s.add_argument("--survey", action="store_true", help="settle every physics model of the selection")
    s = add("convert-model")
    s.add_argument("fdid", type=int)
    s.add_argument("--out", help="archive path for the result (default under Item\\ObjectComponents\\<ns>)")
    sub.add_parser("selftest")

    a = ap.parse_args()
    if a.cmd == "init":
        return cmd_init(a)
    if a.cmd == "selftest":
        import importlib
        import unittest
        tdir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tests")
        sys.path.insert(0, tdir)
        suite = unittest.TestSuite()
        for f in sorted(os.listdir(tdir)):
            if f.startswith("test_") and f.endswith(".py"):
                suite.addTests(unittest.defaultTestLoader.loadTestsFromModule(importlib.import_module(f[:-3])))
        r = unittest.TextTestRunner(verbosity=1).run(suite)
        return 0 if r.wasSuccessful() else 1
    prj = bpcore.Project(a.work)
    log = bpcore.Log(prj, a.cmd)
    # which runs tick the ledger: the full step only (a partial run proves nothing)
    step = a.cmd if a.cmd in ORDER else None
    if a.cmd == "newtech" and a.learn:
        step = None
    if a.cmd == "measure" and a.only:
        step = None
    if a.cmd in ("build", "sheet") and a.display:
        step = None
    if step:
        why = gate_step(prj, step)
        if why:
            log("REFUSED:", why)
            return 3
    rc = dispatch(a, prj, log)
    if step:
        record_step(prj, step, rc == 0)
        log("step %s: %s (recorded in reports/steps.json)" % (step, "PASS" if rc == 0 else "FAIL"))
    return rc


def dispatch(a, prj, log) -> int:
    if a.cmd == "scan":
        import scan
        return scan.scan(prj, log)
    if a.cmd == "newtech":
        import newtech
        return newtech.run(prj, log, learn=a.learn)
    if a.cmd == "find":
        import resolve
        return resolve.find(prj, log, a.text, a.limit)
    if a.cmd == "select":
        import resolve
        return resolve.select(prj, log, a)
    if a.cmd == "fetch":
        import resolve
        return resolve.fetch(prj, log)
    if a.cmd == "measure":
        import measure
        return measure.run(prj, log, a.only)
    if a.cmd == "build":
        import build
        return build.run(prj, log, set(a.display), a.force, a.extension)
    if a.cmd == "tables":
        import tables
        return tables.run(prj, log)
    if a.cmd == "audit":
        import gates
        return gates.run(prj, log)
    if a.cmd == "prove-gates":
        import provegates
        return provegates.run(prj, log)
    if a.cmd == "pack":
        import pack
        return pack.run(prj, log)
    if a.cmd == "sheet":
        import sheet
        return sheet.run(prj, log, set(a.display))
    if a.cmd == "status":
        import status
        return status.run(prj, log)
    if a.cmd == "phys":
        import physcmd
        return physcmd.run(prj, log, a.fdid, a.settle, a.survey)
    if a.cmd == "convert-model":
        import convert
        return convert.cli(prj, log, a.fdid, a.out)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
