r"""
sheet.py - `bp.py sheet`: pictures to LOOK at (the audit cannot see taste,
only rules). One PNG per display in <work>/sheets/:

  head      the helm on every PLAYABLE client body, front and three-quarter
            (body grey, helm textured): floating, sunk, turned, too big/small
  shoulder  left + right model, textured, front and side
  weapon    the model, textured, two angles
  body slots (chest, legs, feet, hands, wrist, waist, shirt, tabard, back)
            every region texture as the client will composite it, labelled
A tile with a RED border is something the sheet itself could not draw (a
missing file, an unreadable texture): read the label and fix the cause.

Open every PNG (an agent: with its image-reading tool) and describe what you
see. A sheet never replaces the user's in-game check.
"""
from __future__ import annotations

import os
import struct

import numpy as np
from PIL import Image, ImageDraw

import blp
import bodies as BD
import bpcore
import m2model as M
import m2_view as MV
import retail_m2 as RM
import scan as SC

REGION_DIR = ("ArmUpperTexture", "ArmLowerTexture", "HandTexture", "TorsoUpperTexture",
              "TorsoLowerTexture", "LegUpperTexture", "LegLowerTexture", "FootTexture")
TILE = 256


def _strip_effects(body: bytes) -> bytes:
    b = bytearray(body)
    struct.pack_into("<IIII", b, 0x120, 0, 0, 0, 0)
    return bytes(b)


class Drawn:
    """A built model ready to draw: bind-pose positions, triangles, uvs and a
    texture per triangle (None = untextured)."""

    def __init__(self, root: str, rel: str, row_tex: str | None, folder: str):
        body = open(os.path.join(root, rel), "rb").read()
        skin = RM.skin(open(os.path.join(root, rel[:-3] + "00.skin"), "rb").read())
        m = M.read(_strip_effects(body))
        mats = np.array([np.eye(4)] * max(1, len(m.a["bones"])))
        geos = {s["id"] for s in skin["subs"]}
        self.pos, self.tris, self.uv, subs = MV.mesh(m, skin, geos, mats)
        texs = []
        n, o = struct.unpack_from("<II", body, 0x50)
        for i in range(n):
            typ, _fl, ln, no = struct.unpack_from("<IIII", body, o + i * 16)
            name = body[no:no + ln].split(b"\0")[0].decode("latin1") if ln > 1 else ""
            if typ == 0 and name:
                texs.append(os.path.join(root, name))
            elif typ == 2 and row_tex:
                texs.append(os.path.join(root, "Item\\ObjectComponents\\%s\\%s.blp" % (folder, row_tex)))
            else:
                texs.append(None)
        nl, ol = struct.unpack_from("<II", body, 0x80)
        look = struct.unpack_from("<%dh" % nl, body, ol) if nl else ()
        tex_of_sub = {}
        for bt in skin["batches"]:
            if bt[3] not in tex_of_sub and bt[9] < len(look) and 0 <= look[bt[9]] < len(texs):
                tex_of_sub[bt[3]] = texs[look[bt[9]]]
        self.cache = {}
        self.face_tex = {}
        for ti, si in enumerate(subs):
            p = tex_of_sub.get(int(si))
            if p and p not in self.cache:
                try:
                    self.cache[p] = blp.decode(open(p, "rb").read()).astype(np.float32)
                except Exception:   # noqa: BLE001
                    self.cache[p] = None
            self.face_tex[ti] = self.cache.get(p) if p else None
        self.problems = [p for p, v in self.cache.items() if v is None]


def _draw(items, size=TILE, yaw=0.0, frame=None):
    """items: [(pos, tris, uv, face_tex dict or None, flat colour or None)]"""
    allp = np.concatenate([i[0] for i in items if len(i[0])])
    surface = None
    if frame is None:
        c, s = np.cos(yaw), np.sin(yaw)
        y = allp[:, 0] * s + allp[:, 1] * c
        frame = (-y.max(), -y.min(), allp[:, 2].min(), allp[:, 2].max())
    img = None
    for pos, tris, uv, ft, flat in items:
        if not len(tris):
            continue
        res = MV.render(pos, tris, size=size, yaw=yaw, frame=frame, uv=uv,
                        face_ids=np.arange(len(tris)) if ft else None,
                        texture_by_face=ft, texture_alpha=True, flat_color=flat,
                        surface=surface, return_depth=True)
        img, _fr, zb = res
        surface = (img, zb)
    return img if img is not None else np.full((size, size, 3), 40, np.uint8)


def _tile(img, label, bad=False):
    im = Image.fromarray(img).convert("RGB")
    d = ImageDraw.Draw(im)
    d.rectangle([0, 0, im.width - 1, 14], fill=(0, 0, 0))
    d.text((3, 1), label[:40], fill=(255, 255, 255))
    if bad:
        d.rectangle([0, 0, im.width - 1, im.height - 1], outline=(255, 0, 0), width=4)
    return im


def _grid(tiles, cols=6):
    rows = (len(tiles) + cols - 1) // cols
    out = Image.new("RGB", (cols * TILE, max(1, rows) * TILE), (20, 20, 20))
    for i, t in enumerate(tiles):
        out.paste(t, ((i % cols) * TILE, (i // cols) * TILE))
    return out


def head_sheet(prj, sc, p, root):
    stem = p["row"]["model"][0][:-4]
    tiles = []
    for race in sc["races"]:
        if not race.get("playable"):
            continue
        for sx in sorted(race["bodies"]):
            rel = "Item\\ObjectComponents\\Head\\%s_%s%s.m2" % (stem, race["prefix"], sx)
            label = "%s%s" % (race["prefix"], sx)
            try:
                cb = BD.client_body(prj, race, sx)
                d = Drawn(root, rel, p["row"]["tex"][0], "Head")
                hp = cb.helm_point() if cb.helm_point() is not None else np.zeros(3)
                hpos = d.pos + hp
                # body: default geosets (id < 100 or x01), grey
                skin = RM.skin(BD.client(prj).read(race["bodies"][sx]["path"][:-3] + "00.skin"))
                # the bare body: geoset 0 plus each group's first variant (group 0
                # holds every hairstyle at once - and a helm hides hair anyway)
                geos = {0} | {g for g in cb.geosets if g >= 100 and g % 100 == 1}
                tris = []
                for s in skin["subs"]:
                    if s["id"] in geos:
                        tris.extend(skin["tris"][s["istart"]:s["istart"] + s["icount"]])
                btris = np.array(tris, dtype=np.int64).reshape(-1, 3)
                bpos = np.array([cb.pos[i] for i in skin["lookup"]])
                head = cb.head_pivot() if cb.head_pivot() is not None else hp
                # frame on the head: 0.9 m box around it
                for yaw in (0.0, 0.9):
                    c, s = np.cos(yaw), np.sin(yaw)
                    yh = head[0] * s + head[1] * c
                    frame = (-yh - 0.45, -yh + 0.45, head[2] - 0.5, head[2] + 0.4)
                    img = _draw([(bpos, btris, None, None, (150, 150, 150)),
                                 (hpos, d.tris, d.uv, d.face_tex, None)], yaw=yaw, frame=frame)
                    tiles.append(_tile(img, label + (" front" if yaw == 0 else " 3/4"), bool(d.problems)))
            except Exception as ex:   # noqa: BLE001
                tiles.append(_tile(np.full((TILE, TILE, 3), 40, np.uint8), "%s %s" % (label, ex), True))
    return _grid(tiles)


def model_sheet(root, p, folder):
    tiles = []
    for k in (0, 1):
        name = p["row"]["model"][k]
        if not name:
            continue
        rel = "Item\\ObjectComponents\\%s\\%s.m2" % (folder, name[:-4])
        try:
            d = Drawn(root, rel, p["row"]["tex"][k], folder)
            for yaw in (0.0, 1.57):
                img = _draw([(d.pos, d.tris, d.uv, d.face_tex, None)], yaw=yaw)
                tiles.append(_tile(img, "%s %d %s" % (folder, k, "front" if yaw == 0 else "side"), bool(d.problems)))
        except Exception as ex:   # noqa: BLE001
            tiles.append(_tile(np.full((TILE, TILE, 3), 40, np.uint8), str(ex), True))
    return _grid(tiles, cols=4)


def texture_sheet(root, p):
    tiles = []
    names = list(p["row"]["regions"])
    folder = "Cape" if p["slot"] == "back" else None
    for i, n in enumerate(names):
        if not n:
            continue
        for suf in ("U", "M", "F"):
            f = os.path.join(root, "Item\\TextureComponents\\%s\\%s_%s.blp" % (REGION_DIR[i], n, suf))
            if os.path.isfile(f):
                try:
                    img = blp.decode(open(f, "rb").read())
                    bg = np.full((*img.shape[:2], 3), 40, np.float32)
                    a = img[..., 3:4] / 255.0
                    rgb = (img[..., :3] * a + bg * (1 - a)).astype(np.uint8)
                    im = Image.fromarray(rgb).resize((TILE, TILE))
                    tiles.append(_tile(np.asarray(im), "%s_%s" % (REGION_DIR[i][:-7], suf)))
                except Exception as ex:   # noqa: BLE001
                    tiles.append(_tile(np.full((TILE, TILE, 3), 40, np.uint8), str(ex), True))
    if folder and p["row"]["tex"][0]:
        f = os.path.join(root, "Item\\ObjectComponents\\Cape\\%s.blp" % p["row"]["tex"][0])
        img = blp.decode(open(f, "rb").read())
        tiles.append(_tile(np.asarray(Image.fromarray(img[..., :3]).resize((TILE, TILE))), "cape texture"))
    return _grid(tiles, cols=4) if tiles else None


def run(prj, log, only: set) -> int:
    plan = bpcore.load_json(prj.path("build", "plan.json"))
    sc = SC.load(prj)
    root = prj.build_dir
    n = 0
    for d, p in sorted(plan.items(), key=lambda x: int(x[0])):
        if not p.get("ok") or (only and d not in only):
            continue
        slot = p["slot"]
        if slot == "head" and p["row"]["model"][0]:
            im = head_sheet(prj, sc, p, root)
        elif slot == "shoulder":
            im = model_sheet(root, p, "Shoulder")
        elif p["row"]["model"][0]:
            im = model_sheet(root, p, "Shield" if p.get("inv") == 14 else "Weapon")
        else:
            im = texture_sheet(root, p)
        if im is None:
            continue
        name = (p["items"][0]["name"] if p.get("items") else "").replace(" ", "_").replace("'", "")[:40]
        out = prj.path("sheets", "%s_%s_%s.png" % (d, slot, name))
        im.save(out)
        n += 1
        log("  %s" % out)
    log("sheet: %d pictures in %s - OPEN EVERY ONE and look (floating, sunk, white, black, green, torn)"
        % (n, prj.path("sheets", "x")[:-2]))
    return 0
