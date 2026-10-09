"""
wago.py - retail tables and files from wago.tools, pinned to ONE build.

  latest_build()            newest live retail build ("wow" product)
  Db2(prj).table("Item")    rows of a retail DB2 table as dicts (downloads once)
  Files(prj).read(fdid)     bytes of a retail file (downloads once, keeps it)
  listfile(prj)             {fdid: path} from the community listfile

ONE BUILD, ALWAYS. Without a version wago answers with the newest copy of a
file in ANY product (classic, beta, PTR). A model from one build with .anim
files from another reads another build's keyframes - every animation then
plays garbage (measured: bone 3 of anim 133 had 18 keys in the model and 21 in
the .anim). Tables and files are therefore always fetched with the SAME build.

A file the CDN serves as zeros is ENCRYPTED at that build (retail has not
released that content yet): Files.read raises Encrypted and nothing built
from it may ship. A 404/400 is an answer ("no such file in this build"), not a
failure, and is not retried. Every download goes to <name>.part and is
renamed only when complete, so a killed run never leaves a broken file.
"""
from __future__ import annotations

import concurrent.futures as cf
import csv
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request

UA = {"User-Agent": "wow-retail-backport/1"}
BUILDS_URL = "https://wago.tools/api/builds"
DB2_URL = "https://wago.tools/db2/%s/csv?build=%s"
FILE_URL = "https://wago.tools/api/casc/%d?download&version=%s"
LISTFILE_URL = "https://github.com/wowdev/wow-listfile/releases/latest/download/community-listfile.csv"

csv.field_size_limit(1 << 30)


class Encrypted(RuntimeError):
    """The pinned build serves this file as zeros (unreleased, encrypted)."""


class Missing(RuntimeError):
    """The pinned build has no such file."""


def _get(url: str, dest: str, tries: int = 6, timeout: int = 180) -> str:
    """Download url to dest via dest.part. Returns "ok" | "missing" | raises."""
    part = dest + ".part"
    last = ""
    for attempt in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout) as r:
                want = int(r.headers.get("Content-Length") or -1)
                got = 0
                with open(part, "wb") as f:
                    while True:
                        chunk = r.read(1 << 16)
                        if not chunk:
                            break
                        f.write(chunk)
                        got += len(chunk)
            if want >= 0 and got != want:
                raise IOError("short read %d of %d" % (got, want))
            if got == 0:
                raise IOError("empty body")
            os.replace(part, dest)
            return "ok"
        except urllib.error.HTTPError as e:
            if e.code in (400, 404):
                return "missing"
            last = "HTTP %d" % e.code
        except Exception as e:   # noqa: BLE001 - network: retry with backoff
            last = str(e)
        time.sleep(min(60, 3 * (attempt + 1) ** 2))
    try:
        os.remove(part)
    except OSError:
        pass
    raise IOError("download failed after %d tries: %s (%s)" % (tries, url, last))


def latest_build(product: str = "wow") -> str:
    with urllib.request.urlopen(urllib.request.Request(BUILDS_URL, headers=UA), timeout=60) as r:
        d = json.loads(r.read())
    rows = d.get(product) or []
    if not rows:
        raise RuntimeError("wago lists no builds for product %r" % product)
    return rows[0]["version"]


class Db2:
    """Retail DB2 tables as CSV, one folder per build."""

    def __init__(self, prj):
        self.prj = prj
        self._cache = {}

    def file(self, name: str) -> str:
        p = os.path.join(self.prj.db2_dir, name + ".csv")
        if not os.path.isfile(p) or os.path.getsize(p) == 0:
            os.makedirs(os.path.dirname(p), exist_ok=True)
            st = _get(DB2_URL % (name, self.prj.build), p)
            if st != "ok":
                raise Missing("table %s does not exist at build %s" % (name, self.prj.build))
        return p

    def header(self, name: str) -> list[str]:
        with open(self.file(name), newline="", encoding="utf-8") as f:
            return next(csv.reader(f))

    def table(self, name: str, keep=None, where=None) -> list[dict]:
        """Rows as dicts (all strings). `keep`: only these columns. `where`:
        a predicate on the row. Big tables are streamed, never all kept."""
        key = (name, tuple(keep) if keep else None) if where is None else None
        if key and key in self._cache:
            return self._cache[key]
        out = []
        with open(self.file(name), newline="", encoding="utf-8") as f:
            rd = csv.DictReader(f)
            missing = [k for k in (keep or ()) if k not in rd.fieldnames]
            if missing:
                raise KeyError("table %s at build %s has no column(s) %s - the layout moved: "
                               "STOP and report (new retail system?)" % (name, self.prj.build, missing))
            for r in rd:
                if where is not None and not where(r):
                    continue
                out.append({k: r.get(k, "") for k in keep} if keep else r)
        if key:
            self._cache[key] = out
        return out


class Files:
    """Retail files by FileDataID, kept under <cache>/<fdid> (+ names.json)."""

    def __init__(self, prj):
        self.prj = prj
        self.dir = prj.cache_dir
        os.makedirs(self.dir, exist_ok=True)
        self._names = None
        self._lock = threading.Lock()

    def path(self, fdid: int) -> str:
        return os.path.join(self.dir, str(int(fdid)))

    def have(self, fdid: int) -> bool:
        return os.path.isfile(self.path(fdid))

    def fetch_one(self, fdid: int) -> str:
        fdid = int(fdid)
        if fdid <= 0:
            return "missing"
        if self.have(fdid):
            return "have"
        try:
            return _get(FILE_URL % (fdid, self.prj.build), self.path(fdid))
        except IOError as e:
            return "FAILED " + str(e)

    def fetch(self, ids, workers=None, quiet=True) -> dict:
        ids = sorted({int(i) for i in ids if int(i) > 0})
        todo = [i for i in ids if not self.have(i)]
        out = {i: "have" for i in ids if i not in todo}
        if not todo:
            return out
        w = workers or int(self.prj.cfg.get("workers") or 6)
        t0 = time.time()
        with cf.ThreadPoolExecutor(max_workers=w) as ex:
            for n, (i, st) in enumerate(zip(todo, ex.map(self.fetch_one, todo)), 1):
                out[i] = st
                if not quiet and (n % 50 == 0 or n == len(todo) or st.startswith("FAILED")):
                    print("  fetched %d/%d (%.0fs)%s" % (n, len(todo), time.time() - t0,
                                                         ("  %d %s" % (i, st)) if st.startswith("FAILED") else ""),
                          flush=True)
        # a second, slow pass for whatever failed: one at a time, longer waits
        # (a busy server or a weak line fails parallel requests that pass alone)
        again = [i for i in todo if out[i].startswith("FAILED")]
        for i in again:
            try:
                out[i] = _get(FILE_URL % (i, self.prj.build), self.path(i), tries=10, timeout=600)
            except IOError as e:
                out[i] = "FAILED " + str(e)
            if not quiet:
                print("  retry %d: %s" % (i, out[i]), flush=True)
        return out

    def read(self, fdid: int) -> bytes:
        """The file's bytes (downloaded on demand). Raises Missing / Encrypted."""
        fdid = int(fdid)
        if not self.have(fdid):
            st = self.fetch_one(fdid)
            if st == "missing":
                raise Missing("file %d does not exist at build %s" % (fdid, self.prj.build))
            if st.startswith("FAILED"):
                raise IOError("file %d: %s" % (fdid, st))
        with open(self.path(fdid), "rb") as f:
            d = f.read()
        if not d.strip(b"\0"):
            raise Encrypted("file %d is encrypted at build %s (served as zeros: unreleased content)"
                            % (fdid, self.prj.build))
        return d

    def try_read(self, fdid: int):
        try:
            return self.read(fdid)
        except (Missing, Encrypted, IOError):
            return None


_LF = {}


def listfile(prj) -> dict:
    """{fdid: lower-case path} (downloaded once into <work>/listfile.csv)."""
    p = prj.cfg.get("listfile") or os.path.join(prj.work, "listfile.csv")
    if p in _LF:
        return _LF[p]
    if not os.path.isfile(p):
        print("downloading the community listfile (~150 MB, once)...", flush=True)
        if _get(LISTFILE_URL, p) != "ok":
            raise IOError("community listfile not available")
    out = {}
    with open(p, encoding="utf-8", errors="replace") as f:
        for line in f:
            i = line.find(";")
            if i > 0 and line[:i].isdigit():
                out[int(line[:i])] = line[i + 1:].strip().lower()
    _LF[p] = out
    return out


def stem(prj, fdid: int) -> str:
    """Retail's own file stem (no folder, no extension), else f<fdid>."""
    p = listfile(prj).get(int(fdid), "")
    p = p.replace("\\", "/").rsplit("/", 1)[-1]
    p = re.sub(r"\.(m2|blp|skin|anim|skel|phys)$", "", p)
    return p or "f%d" % int(fdid)
