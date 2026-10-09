"""
client.py - read a 3.3.5a client's files exactly as the client itself does.

    arc = Client(r"C:\\WoW\\Data")
    data = arc.read("DBFilesClient\\ItemDisplayInfo.dbc")   # the WINNING copy
    arc.who("DBFilesClient\\ItemDisplayInfo.dbc")            # which archive won

THE PRIORITY RULE (read off wow.exe 3.3.5a 12340, not remembered)
  0x00405AB0  PatchFiles::Enumerate collects Data\\patch-?.MPQ and
              Data\\<locale>\\patch-<locale>-?.MPQ, then qsorts them with the
              comparator at 0x00401200 = -SStrCmpI(a, b): DESCENDING by full
              path.
  0x00405E2E  the mount loop walks that vector BACKWARDS, opening each at
              priority 0x40, 0x41, ... so the vector's FIRST element (the
              highest name, e.g. patch-z.mpq) ends up highest.
  0x00406031  the base archives follow at priority 0x3F, decreasing, in the
              mount table's order at 0x00AB6168 - every patch outranks every
              base archive.
Only names matching patch-?.MPQ (ONE character) are patches on a stock client.
A file whose name ends in anything else (patch-7.mpq.disabled, backup-*.mpq
not in the table) is NOT mounted and is ignored here too.
Some realms ship a modified wow.exe whose pattern is patch-*.MPQ (any length);
pass wildcard_patches=True for those (the report says which rule was used).

Uses only this kit's own MPQ reader (mpq_read.py: the client's sector rules).
A file the reader cannot decode (an exotic compression) is reported, never
silently treated as absent.
"""
from __future__ import annotations

import os
import re

from mpq_read import MpqReader

# wow.exe 0x00AB6168, highest priority first ("locale-" style entries are
# prefixes matched against <name>-<locale>.mpq)
BASE_ORDER = [
    "alternate.mpq", "interface.mpq", "misc.mpq", "model.mpq", "texture.mpq",
    "terrain.mpq", "wmo.mpq", "sound.mpq", "fonts.mpq", "dbc.mpq",
    "speech.mpq", "expansionloc.mpq", "lichkingloc.mpq", "expansionspeech.mpq",
    "lichkingspeech.mpq", "expansion.mpq", "lichking.mpq", "common.mpq",
    "common-2.mpq", "locale-", "speech-", "expansion-locale-",
    "lichking-locale-", "expansion-speech-", "lichking-speech-",
    "development.mpq", "streaming.mpq", "streamingloc.mpq",
]


def _base_rank(low: str):
    for i, key in enumerate(BASE_ORDER):
        if key.endswith(".mpq"):
            if low == key:
                return i
        elif low.startswith(key):
            return i
    return None          # not in the client's mount table: never mounted


class Client:
    def __init__(self, data_dir: str, locale: str | None = None, wildcard_patches: bool = False):
        self.data_dir = data_dir
        if not os.path.isdir(data_dir):
            raise FileNotFoundError("client Data folder not found: %s" % data_dir)
        locs = [d for d in os.listdir(data_dir)
                if os.path.isdir(os.path.join(data_dir, d)) and re.fullmatch(r"[a-zA-Z]{4}", d)]
        if locale is None:
            locale = locs[0] if locs else ""
        self.locale = locale
        ldir = next((os.path.join(data_dir, d) for d in locs if d.lower() == locale.lower()), None)
        pat = r"patch-[a-z0-9]+\.mpq" if wildcard_patches else r"patch-[a-z0-9]\.mpq"
        lpat = (r"patch-%s-[a-z0-9]+\.mpq" if wildcard_patches else r"patch-%s-[a-z0-9]\.mpq") % locale.lower()
        patches, base, skipped = [], [], []
        for d, rx_patch in ((data_dir, pat), (ldir, lpat)):
            if not d:
                continue
            for n in os.listdir(d):
                p = os.path.join(d, n)
                low = n.lower()
                if not os.path.isfile(p):
                    continue
                if not low.endswith(".mpq"):
                    if ".mpq" in low:
                        skipped.append(p)
                    continue
                if re.fullmatch(rx_patch, low):
                    patches.append(p)
                elif low in ("patch.mpq", "patch-%s.mpq" % locale.lower()):
                    base.append((-1, p))
                else:
                    r = _base_rank(low)
                    if r is None:
                        skipped.append(p)
                    else:
                        base.append((r, p))
        # descending by FULL path, as the client's qsort does
        patches.sort(key=lambda p: p.lower(), reverse=True)
        base.sort(key=lambda x: x[0])
        self.order = patches + [p for _r, p in base]
        self.skipped = skipped
        self.wildcard = wildcard_patches
        self.archives = []
        self.errors = []
        for p in self.order:
            try:
                self.archives.append((p, MpqReader(p)))
            except Exception as ex:   # noqa: BLE001 - reported, the client would skip it too
                self.errors.append("%s: %s" % (p, ex))

    def read(self, path: str):
        """Bytes of the winning copy of `path`, or None when no archive has it."""
        got = self.who(path, data=True)
        return got[1] if got else None

    def who(self, path: str, data: bool = False):
        """(archive path, bytes or None) of the winning copy, or None."""
        name = path.replace("/", "\\")
        for p, a in self.archives:
            try:
                if a.locate(name) is None:
                    continue
            except Exception:
                continue
            if not data:
                return p, None
            try:
                return p, a.read(name)
            except Exception as ex:   # noqa: BLE001
                raise RuntimeError("%s holds %s but it cannot be decoded here: %s" % (p, name, ex))
        return None

    def exists(self, path: str) -> bool:
        return self.who(path) is not None

    def listfiles(self):
        """Every name the mounted archives list in their (listfile)s (a file
        an archive holds without listing it is still readable by name)."""
        seen = {}
        for p, a in self.archives:
            try:
                lf = a.read("(listfile)")
            except Exception:
                lf = None
            if not lf:
                continue
            for x in lf.decode("latin-1").replace("\r", "").split("\n"):
                x = x.strip()
                if x:
                    seen.setdefault(x.lower(), x)
        return seen
