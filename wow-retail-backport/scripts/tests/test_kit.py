"""The kit's own tests: no network, no client, no retail files needed.

    python bp.py selftest

Each test pins one rule that once broke something in game (named in its
docstring). A failing test means the kit is broken: do not use it until fixed.
"""
from __future__ import annotations

import json
import os
import shutil
import struct
import sys
import tempfile
import unittest

import numpy as np

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
import _toolpath  # noqa: E402,F401

import blp  # noqa: E402
import bpcore  # noqa: E402
from client import Client  # noqa: E402
from mpq_read import MpqReader  # noqa: E402
from mpq_write import MpqWriter  # noqa: E402
from wdbc import Wdbc  # noqa: E402


def project(tmp):
    cfg = dict(bpcore.DEFAULTS, build="0.0.0.1", client_data=tmp)
    with open(os.path.join(tmp, "bp.json"), "w") as f:
        json.dump(cfg, f)
    return bpcore.Project(tmp)


def wdbc_bytes(rows, strings=b"\0", fields=3):
    out = struct.pack("<4s4I", b"WDBC", len(rows), fields, fields * 4, len(strings))
    for r in rows:
        out += struct.pack("<%dI" % fields, *r)
    return out + strings


class Tables(unittest.TestCase):
    """DBC rows are APPENDED to the client's own table; a client row is never
    replaced (never override the game)."""

    def test_append_roundtrip(self):
        t = Wdbc(wdbc_bytes([[1, 1, 7], [5, 0, 9]], b"\0Hu\0"))
        self.assertEqual(t.string(t.rows[0][1]), "Hu")
        t.add([9, 0, 3], strings={1: "Backport\\x.mdx"})
        t2 = Wdbc(t.to_bytes())
        self.assertEqual(sorted(t2.ids()), [1, 5, 9])
        self.assertEqual(t2.string(t2.by_id()[9][1]), "Backport\\x.mdx")
        self.assertEqual(t2.by_id()[1], [1, 1, 7])
        with self.assertRaises(ValueError):
            t.add([5, 0, 0])

    def test_packed_layout_refused(self):
        bad = struct.pack("<4s4I", b"WDBC", 0, 3, 10, 1) + b"\0"
        with self.assertRaises(ValueError):
            Wdbc(bad)


class Archives(unittest.TestCase):
    """The output archive reads back byte for byte, and the client's own
    priority rule decides which copy of a file wins (wow.exe 0x00405AB0:
    patches descending by full path, then patch.MPQ, then the base table)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def mpq(self, path, files):
        w = MpqWriter(path)
        for k, v in files.items():
            w.add(k, v)
        w.close()

    def test_roundtrip(self):
        p = os.path.join(self.tmp, "a.mpq")
        data = {"Item\\A.m2": os.urandom(50000), "Item\\B.blp": b"x" * 9000, "Empty.txt": b""}
        self.mpq(p, data)
        r = MpqReader(p)
        for k, v in data.items():
            self.assertEqual(r.read(k), v)
        r.fh.close()

    def test_client_priority(self):
        d = os.path.join(self.tmp, "Data")
        os.makedirs(os.path.join(d, "enUS"))
        self.mpq(os.path.join(d, "common.MPQ"), {"x.txt": b"base", "only_base.txt": b"b"})
        self.mpq(os.path.join(d, "patch.MPQ"), {"x.txt": b"patch"})
        self.mpq(os.path.join(d, "patch-2.MPQ"), {"x.txt": b"p2"})
        self.mpq(os.path.join(d, "patch-y.MPQ"), {"x.txt": b"py"})
        self.mpq(os.path.join(d, "patch-7.MPQ.disabled"), {"x.txt": b"never"})
        self.mpq(os.path.join(d, "enUS", "patch-enUS-z.MPQ"), {"x.txt": b"locale"})
        c = Client(d, "enUS")
        names = [os.path.basename(p).lower() for p in c.order]
        # root patches sort above locale patches ("data\\p" > "data\\enus\\p")
        self.assertEqual(names[:3], ["patch-y.mpq", "patch-2.mpq", "patch-enus-z.mpq"])
        self.assertEqual(c.read("x.txt"), b"py")
        self.assertEqual(c.read("only_base.txt"), b"b")
        self.assertTrue(any("disabled" in s for s in c.skipped))


class Textures(unittest.TestCase):
    """> 1024 px corrupts the client's heap; BLP encoding 3 overruns a memcpy;
    a DXT body-region texture draws GREEN on the 3.3.5 compositor."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.prj = project(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_limits(self):
        from convert import Builder
        b = Builder(self.prj)
        img = np.zeros((16, 2048, 4), np.uint8)
        img[..., 3] = 255
        big = blp.encode(img, "dxt1")
        out, what = b.tex335(big)
        w, h = struct.unpack_from("<II", out, 12)
        self.assertEqual((w, h), (1024, 8))
        self.assertIn("dropped 1", what)
        small = blp.encode(np.zeros((64, 64, 4), np.uint8), "dxt5")
        self.assertEqual(b.tex335(small)[0], small)

    def test_green_gate(self):
        import gates
        rep = gates.Report()
        img = np.full((64, 64, 4), 200, np.uint8)
        gates.texture_checks(rep, "Item\\TextureComponents\\FootTexture\\Backport\\1_U.blp",
                             blp.encode(img, "dxt1"), 1024)
        self.assertIn("T2 green", rep.fails)
        rep = gates.Report()
        gates.texture_checks(rep, "Item\\TextureComponents\\FootTexture\\Backport\\1_U.blp",
                             blp.encode_palette(img), 1024)
        self.assertFalse(rep.fails)

    def test_palette_alpha(self):
        """Pillow reads palettised BLP alpha as 0 (retail Pandaren underwear
        went invisible): the kit decodes encoding 1 itself."""
        img = np.zeros((8, 8, 4), np.uint8)
        img[..., 0] = 120
        img[..., 3] = 128
        back = blp.decode(blp.encode_palette(img))
        self.assertTrue((back[..., 3] == 128).all())


class Rules(unittest.TestCase):
    def test_sheath(self):
        """Retail SheatheType 0..8 -> 3.3.5 0..7 (measured on ~26,000 shared items)."""
        from build import SHEATH_335
        self.assertEqual([SHEATH_335[i] for i in range(9)], [0, 1, 2, 3, 4, 0, 6, 7, 7])

    def test_subclass_fallbacks(self):
        from tables import SUBCLASS_FALLBACK
        self.assertEqual(SUBCLASS_FALLBACK[(2, 9)][:2], (2, 7))
        self.assertEqual(SUBCLASS_FALLBACK[(4, 5)][:2], (4, 0))

    def test_strip_race(self):
        from build import strip_race
        self.assertEqual(strip_race("helm_plate_x_hu_m"), "helm_plate_x")
        self.assertEqual(strip_race("plate_raiddk_d_01_helm"), "plate_raiddk_d_01_helm")

    def test_geoset_value(self):
        """A retail variant the client body lacks becomes the measured nearest
        one (boots 506..510 on a 3.3.5 body: the leg vanished)."""
        import measure
        tmp = tempfile.mkdtemp()
        try:
            prj = project(tmp)
            bpcore.save_json(prj.path("client", "geoset_map.json"), {"5": {"510": 502}})
            self.assertEqual(measure.client_value(prj, 5, 9), 1)
            self.assertEqual(measure.client_value(prj, 5, 2), 2)
            self.assertEqual(measure.client_value(prj, 5, -1), 0)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_baked_tag(self):
        from convert import baked_tag
        self.assertEqual(baked_tag({}, 0), "")
        self.assertEqual(baked_tag({3: 5, 24: 7}, 0), baked_tag({24: 7, 3: 5}, 0))
        self.assertNotEqual(baked_tag({3: 5}, 0), baked_tag({3: 5}, 11))


class NewTech(unittest.TestCase):
    def test_features(self):
        import newtech
        f = newtech.features(blp.encode(np.zeros((4, 4, 4), np.uint8), "dxt5"))
        self.assertEqual(f["blp.format"], {"1/2/8/7"})
        self.assertEqual(newtech.bits(0x8 | 0x200), [8, 512])

    def test_registry_ships(self):
        reg = json.load(open(os.path.join(HERE, "data", "known_tech.json")))
        for k in ("m2.chunk", "m2.texture_type", "blp.format", "db2.ItemDisplayInfo"):
            self.assertIn(k, reg)
        self.assertIn("MD21", reg["m2.chunk"])


if __name__ == "__main__":
    unittest.main()
