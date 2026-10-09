"""
status.py - `bp.py status`: the ticked checklist (reports/steps.json) and the
ONE next command. A step is ticked only by its own full run passing; running
an earlier step again un-ticks everything after it.
"""
from __future__ import annotations

import bpcore

ORDER = ["scan", "select", "fetch", "newtech", "measure", "build", "tables", "audit", "prove-gates",
         "pack", "sheet"]
WHAT = {"scan": "the client is read", "select": "what to port is resolved", "fetch": "every retail file is here",
        "newtech": "nothing retail uses is unknown", "measure": "this client's bodies are measured",
        "build": "every display converted", "tables": "client + server tables and SQL written",
        "audit": "every gate passes", "prove-gates": "every gate catches its planted defect",
        "pack": "archives written and read back", "sheet": "pictures drawn (now LOOK at every one)"}


def run(prj, log) -> int:
    led = bpcore.load_json(prj.path("reports", "steps.json"), {})
    log("project %s  (retail build %s, client %s)" % (prj.work, prj.build, prj.cfg.get("client_data")))
    nxt = None
    for s in ORDER:
        r = led.get(s)
        mark = "x" if r and r["ok"] else ("!" if r else " ")
        log("  [%s] %-12s %-42s %s" % (mark, s, WHAT[s], ("%s %s" % ("PASS" if r["ok"] else "FAIL", r["at"])) if r else ""))
        if (not r or not r["ok"]) and nxt is None:
            nxt = s
    if prj.cfg and nxt:
        log("next: python bp.py %s --work \"%s\"   (re-read its row in SKILL.md first)" % (nxt, prj.work))
    else:
        log("every step PASSED. Last: look at every sheet, then hand out/INSTALL.txt + the in-game "
            "checklist (references/troubleshooting.md) to the user")
    return 0
