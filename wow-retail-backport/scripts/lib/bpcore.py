"""
bpcore.py - one project's settings, folders and record files.

A PROJECT is one folder (the "work" folder) holding everything a backport
produces. `bp.py init` writes <work>/bp.json; every other command reads it.

<work>/
  bp.json                 settings (below) - the only file you edit by hand
  db2/<build>/<Table>.csv retail tables at the pinned build
  cache/<build>/<fdid>    retail files at the pinned build (+ names.json)
  listfile.csv            community listfile (fdid;path)
  client/scan.json        what the target client is (races, prefixes, ids, archives)
  client/dbc/*.dbc        the client's WINNING copy of every table we extend
  client/helm_shift.json, client/geoset_map.json   measured once per client
  selection.json          what you asked for, resolved to displays and files
  build/                  converted files at their archive paths + manifest.json
  ids.json                retail display/item -> new ids (stable across runs)
  out/                    THE RESULT: patch MPQ, DBCs (client + server), SQL
  reports/*.json          every gate's findings
  sheets/*.png            pictures to look at
  logs/                   one log per command run
"""
from __future__ import annotations

import json
import os
import threading
import time

DEFAULTS = {
    "build": "",                 # pinned retail build, e.g. "12.1.0.69933" (init fills the newest live one)
    "client_data": "",           # the target 3.3.5a client's Data folder (read only, never written)
    "locale": "",                # client locale folder (enUS, deDE...); "" = the one found
    "wildcard_patches": False,   # True only for a client whose wow.exe mounts patch-*.MPQ (long names)
    "namespace": "Backport",     # private sub-folder under every art path we add
    "icon_prefix": "bp",         # icon names: Interface\\Icons\\<prefix>_<fdid>.blp
    "patch_name": "",            # output archive; "" = the first free patch-<letter>.MPQ above the client's
    "display_id_start": 0,       # 0 = first free id above the client's ItemDisplayInfo, rounded up to 1000
    "item_id_start": 0,          # 0 = 900000 (set it to a range your server does not use)
    "helmvis_id_start": 0,       # 0 = first free id above the client's HelmetGeosetVisData
    "server": "azerothcore",     # azerothcore | trinitycore (SQL dialect of item_template)
    "race_map": {},              # {"<client race id>": <retail race id>} for custom races the scan cannot match
    "cache": "",                 # optional shared retail file cache folder (same layout as cache/<build>)
    "listfile": "",              # optional path to an existing community listfile
    "workers": 6,                # parallel downloads
    "max_texture": 1024,         # the client's texture limit (crash above it): do not raise
}

_lock = threading.Lock()


class Project:
    def __init__(self, work: str):
        self.work = os.path.abspath(work)
        p = os.path.join(self.work, "bp.json")
        if not os.path.isfile(p):
            raise SystemExit("no project at %s - run: python bp.py init --work \"%s\" --client <Data folder>"
                             % (self.work, self.work))
        cfg = dict(DEFAULTS)
        cfg.update(load_json(p))
        self.cfg = cfg
        if not cfg["build"]:
            raise SystemExit("bp.json has no build: run init again or set \"build\"")

    # folders ---------------------------------------------------------------
    def path(self, *parts) -> str:
        p = os.path.join(self.work, *parts)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        return p

    @property
    def build(self) -> str:
        return self.cfg["build"]

    @property
    def db2_dir(self) -> str:
        return self.path("db2", self.build, "x")[:-2]

    @property
    def cache_dir(self) -> str:
        c = self.cfg.get("cache") or ""
        return c if c else self.path("cache", self.build, "x")[:-2]

    @property
    def build_dir(self) -> str:
        return self.path("build", "x")[:-2]

    @property
    def ns(self) -> str:
        return self.cfg["namespace"]


def save_json(path: str, obj) -> None:
    """Atomic write: a temp file per process, then a retried replace (Windows
    refuses a replace onto a file another process holds open)."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = "%s.%d.tmp" % (path, os.getpid())
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=1, sort_keys=True)
    for attempt in range(30):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            time.sleep(0.2 * (attempt + 1))
    raise PermissionError("could not replace %s" % path)


def load_json(path: str, default=None):
    if not os.path.isfile(path):
        if default is not None:
            return default
        raise FileNotFoundError(path)
    with open(path, encoding="utf-8") as f:
        return json.load(f)


class Log:
    """Prints and appends to <work>/logs/<name>.log."""

    def __init__(self, prj: Project, name: str):
        self.f = open(prj.path("logs", name + ".log"), "a", encoding="utf-8")
        self.f.write("\n=== %s ===\n" % time.strftime("%Y-%m-%d %H:%M:%S"))

    def __call__(self, *a):
        s = " ".join(str(x) for x in a)
        print(s, flush=True)
        self.f.write(s + "\n")
        self.f.flush()
