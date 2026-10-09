r"""
convert.py - one retail file -> the file a 3.3.5a client draws. Every builder
goes through here, so one rule fixes every slot.

TEXTURES (tex335)
  BLP2 the client reads: never wider/taller than max_texture (1024 - above it
  the client allocates one mip smaller on the GPU and copies full rows: heap
  corruption), never encoding 3 (a memcpy overrun when the texture-quality
  setting drops a mip), every mip offset inside the file. One copy per
  FileDataID at Item\ObjectComponents\<ns>\<fdid>.blp.
  Body-section textures (the compositor's) are PALETTISED (encoding 1): the
  3.3.5 compositor does not take DXT - a DXT section draws missing-texture green.

MODELS (convert_model)
  m2_port.port: retail's own bytes + a declared list of patches, audited byte
  for byte (m2_port.audit), read back as the client does (check_264). Plus:
  - type-2 slot (the item's own texture) kept LIVE: the DBC row supplies it;
  - retail-only item slots 3/4/24 (ItemDisplayInfoModelMatRes TextureType)
    baked to the texture retail gives THIS display (a colourway with other
    glow gets its own file); a batch still sampling an unbound slot is
    DROPPED (it would draw a flat white sheet);
  - EXP2 colorMult/alphaMult baked into each emitter (retail draws some
    emitters at alpha 0 through it);
  - ParticleColorID baked into the emitter keys (3.3.5's ParticleColor rows
    are other rows), selector cleared - one file per colourway;
  - colour/alpha tracks keyed in SOME sequences only made constant (3.3.5
    evaluates an unkeyed sequence as 0 = black: the "black chest" bug);
  - bounds grown to contain the mesh (the client culls by the box: items
    vanished on camera dips);
  - external .anim written for every sequence whose keys live in one and
    whose id the 3.3.5 client can ask for (<= 505);
  - global flag 0x200 (newer emitter format): emitters dropped, mesh kept,
    reported (m2_port refuses to guess that layout);
  - physics (.phys): shipped in retail's BIND pose (3.3.5 has no runtime
    physics; an offline settle with no body hung helms beside the head).
"""
from __future__ import annotations

import hashlib
import json
import os
import struct
import threading

import blp
import bpcore
import m2_port as MP
import m2edit as E
import m2model as M
import retail_m2 as RM
import verify_bounds as VB
from wago import Db2, Files, stem as wago_stem

BAKE_TYPES = (3, 4, 24)          # retail-only item texture slots 3.3.5 never binds
MAX_ANIM_ID = 505                # the highest animation id 3.3.5's AnimationData knows


class TexError(RuntimeError):
    pass


class Builder:
    """Writes built files under <work>/build/<archive path> and records each in
    build/manifest.json (archive path -> {path, kind, src})."""

    def __init__(self, prj):
        self.prj = prj
        self.files = Files(prj)
        self.db = Db2(prj)
        self.root = prj.build_dir
        self.man_path = os.path.join(self.root, "manifest.json")
        self.man = bpcore.load_json(self.man_path, {})
        self.lock = threading.Lock()
        self.ns = prj.ns
        self.max_edge = int(prj.cfg.get("max_texture") or 1024)
        self._pc = None

    # bookkeeping ---------------------------------------------------------
    def save(self):
        bpcore.save_json(self.man_path, self.man)

    def have(self, rel: str) -> bool:
        return rel.lower() in self.man and os.path.isfile(os.path.join(self.root, rel))

    def write(self, rel: str, data: bytes, kind: str, src) -> None:
        p = os.path.join(self.root, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f:
            f.write(data)
        with self.lock:
            self.man[rel.lower()] = {"path": rel, "kind": kind, "src": src, "size": len(data)}

    def read_built(self, rel: str) -> bytes:
        with open(os.path.join(self.root, rel), "rb") as f:
            return f.read()

    def stem(self, fdid: int) -> str:
        return wago_stem(self.prj, fdid)

    # textures ------------------------------------------------------------
    def tex335(self, d: bytes) -> tuple[bytes, str]:
        if d[:4] != b"BLP2":
            raise TexError("not BLP2")
        typ, enc, _ad, _at, _mips, w, h = struct.unpack_from("<IBBBBII", d, 4)
        offs = list(struct.unpack_from("<16I", d, 20))
        sizes = list(struct.unpack_from("<16I", d, 84))
        levels = [i for i in range(16) if offs[i] and sizes[i]]
        for i in levels:
            if offs[i] + sizes[i] > len(d):
                raise TexError("mip %d past the end of the file" % i)
        if not levels or levels[0] != 0:
            raise TexError("no level 0")
        chain = []
        for i in range(16):
            if i in levels:
                chain.append(i)
            else:
                break
        if enc == 3 or typ != 1 or enc not in (1, 2):
            img = blp.decode(d)
            while img.shape[1] > self.max_edge or img.shape[0] > self.max_edge:
                img = blp.resize(img, max(1, img.shape[1] // 2), max(1, img.shape[0] // 2))
            has_alpha = bool((img[:, :, 3] < 250).any())
            return blp.encode(img, "dxt5" if has_alpha else "dxt1"), "re-encoded encoding %d" % enc
        drop, ww, hh = 0, w, h
        while ww > self.max_edge or hh > self.max_edge:
            ww, hh = max(1, ww // 2), max(1, hh // 2)
            drop += 1
        if drop >= len(chain):
            raise TexError("no mip level <= %d" % self.max_edge)
        keep = chain[drop:]
        head = bytearray(d[:148])
        struct.pack_into("<II", head, 12, ww, hh)
        # the BLP2 header is ALWAYS 148 + a 1024-byte palette block, DXT or
        # not (every Blizzard file): mip data starts at 1172 at the earliest,
        # so a small texture never ends inside the header the client reads
        pal = (d[148:148 + 1024] + bytes(1024))[:1024]
        body = bytearray()
        base = 148 + len(pal)
        noffs, nsizes = [0] * 16, [0] * 16
        for j, i in enumerate(keep):
            noffs[j] = base + len(body)
            nsizes[j] = sizes[i]
            body += d[offs[i]:offs[i] + sizes[i]]
        struct.pack_into("<16I", head, 20, *noffs)
        struct.pack_into("<16I", head, 84, *nsizes)
        head[11] = 1 if len(keep) > 1 else 0
        if not drop and keep == levels and len(d) == base + len(body):
            return d, "as retail"
        return bytes(head) + pal + bytes(body), ("dropped %d top mip(s)" % drop) if drop else "trimmed mip chain"

    def publish_texture(self, fdid: int) -> str:
        rel = "Item\\ObjectComponents\\%s\\%d.blp" % (self.ns, int(fdid))
        if not self.have(rel):
            data, what = self.tex335(self.files.read(int(fdid)))
            self.write(rel, data, "texture", {"fdid": int(fdid), "done": what})
        return rel

    def row_texture(self, fdid: int, folder: str) -> str:
        """A texture a DBC row names (ModelTexture, cape): the client builds
        Item\\ObjectComponents\\<folder>\\<name>.blp. Returns the row's name."""
        name = "%s\\%d" % (self.ns, int(fdid))
        rel = "Item\\ObjectComponents\\%s\\%s.blp" % (folder, name)
        if not self.have(rel):
            src = self.publish_texture(fdid)
            self.write(rel, self.read_built(src), "texture", {"fdid": int(fdid), "copy_of": src})
        return name

    def section_texture(self, fdid: int, rel: str) -> None:
        """A body-section (compositor) texture: palettised, power-of-two, <= max."""
        if self.have(rel):
            return
        img = blp.decode(self.files.read(int(fdid)))
        while img.shape[1] > self.max_edge or img.shape[0] > self.max_edge:
            img = blp.resize(img, img.shape[1] // 2, img.shape[0] // 2)
        self.write(rel, blp.encode_palette(img), "section", {"fdid": int(fdid)})

    def icon(self, fdid: int) -> str:
        """Interface\\Icons\\<prefix>_<fdid>.blp; returns the row's icon name ('' if unusable)."""
        name = "%s_%d" % (self.prj.cfg.get("icon_prefix") or "bp", int(fdid))
        rel = "Interface\\Icons\\%s.blp" % name
        if self.have(rel):
            return name
        try:
            img = blp.decode(self.files.read(int(fdid)))
        except Exception:      # noqa: BLE001 - an icon is never worth failing an item for
            return ""
        # the 3.3.5 icon frame is 64x64; retail ships 64 (or larger) squares
        while img.shape[0] > 64 or img.shape[1] > 64:
            img = blp.resize(img, max(1, img.shape[1] // 2), max(1, img.shape[0] // 2))
        self.write(rel, blp.encode(img, "dxt5" if (img[:, :, 3] < 250).any() else "dxt1"), "icon",
                   {"fdid": int(fdid)})
        return name

    # models --------------------------------------------------------------
    def particle_color_row(self, pc_id: int):
        if self._pc is None:
            self._pc = {}
            for r in self.db.table("ParticleColor"):
                def rgb(v):
                    v = int(v) & 0xFFFFFFFF
                    return ((v >> 16) & 0xFF, (v >> 8) & 0xFF, v & 0xFF)
                self._pc[int(r["ID"])] = [(rgb(r["Start_%d" % k]), rgb(r["MID_%d" % k]), rgb(r["End_%d" % k]))
                                          for k in range(3)]
        return self._pc.get(int(pc_id))

    def uses_particle_color(self, fdid: int) -> bool:
        try:
            body = MP.unwrap(self.files.read(fdid))[0]
        except Exception:      # noqa: BLE001
            return False
        n, o = struct.unpack_from("<II", body, MP.H_PARTICLES)
        return any(struct.unpack_from("<H", body, o + i * MP.SZ_PARTICLE_RETAIL + 0x2A)[0] in (11, 12, 13)
                   for i in range(n))

    def effective_pc(self, fdid: int, pc_id) -> int:
        pc_id = int(pc_id or 0)
        return pc_id if pc_id and self.uses_particle_color(fdid) else 0

    def convert_model(self, fdid: int, rel: str, baked: dict | None = None, kind: str = "model",
                      particle_color: int = 0) -> dict:
        """Retail model `fdid` -> archive path `rel` (+ 00.skin, .anim files)."""
        if self.have(rel):
            return self.man[rel.lower()]["src"]
        raw = self.files.read(int(fdid))
        report = {"fdid": int(fdid)}
        body0, _v, chunks = MP.unwrap(raw)
        flags = struct.unpack_from("<I", body0, MP.H_GLOBAL_FLAGS)[0]
        if flags & MP.GF_NEW_PARTICLE:
            raw = without_new_particles(raw)
            report["particles_dropped"] = "global flag 0x200 emitter format (no 3.3.5 equivalent)"
            body0 = MP.unwrap(raw)[0]
        kinds = texture_kinds(body0)
        fids = align_txid(kinds, RM.deps(raw).get("texture", []))
        paths = []
        rawb = bytearray(raw)
        base = chunks[b"MD21"][0] if chunks else 0
        _n, toff = struct.unpack_from("<II", body0, MP.H_TEXTURES)
        for slot, (k, f) in enumerate(zip(kinds, fids)):
            if k == 0:
                if not f:
                    raise RuntimeError("texture slot %d has no FileDataID" % slot)
                paths.append(self.publish_texture(f))
            elif k in BAKE_TYPES and baked and baked.get(k):
                struct.pack_into("<I", rawb, base + toff + slot * 16, 0)
                paths.append(self.publish_texture(baked[k]))
                report.setdefault("baked", {})[str(k)] = int(baked[k])
            else:
                if k in BAKE_TYPES:
                    report.setdefault("unbaked_slot_types", []).append(k)
                paths.append("")
        unbound = {i for i, (k, p) in enumerate(zip(kinds, paths))
                   if not p and (k in BAKE_TYPES or (k == 1 and kind.startswith("piece")))}
        raw = bytes(rawb)
        sk = RM.deps(raw).get("skin") or []
        if not sk:
            raise RuntimeError("model %d names no skin" % fdid)
        skin = self.files.read(sk[0])
        body, skins, rep = MP.port(raw, texture_paths=paths, skins=[skin], supplied_types=(2,))
        MP.audit(raw, body, rep)
        if unbound:
            skins[0], dropped = E.drop_batches(body, skins[0], unbound)
            if dropped:
                report["batches_unbound_dropped"] = dropped
        body = bytearray(body)
        bake_exp2(raw, body, report)
        if particle_color:
            bake_particle_colour(body, self.particle_color_row(particle_color), particle_color, report)
        body = constant_colours(body, report)
        body, grown = E.union_bounds(bytes(body))
        if grown:
            report["bounds_grown"] = True
        MP.check_264(bytes(body), skins)
        anims = self.external_anims(int(fdid), raw, bytes(body))
        self.write(rel, bytes(body), kind, report)
        self.write(rel[:-3] + "00.skin", skins[0], kind + "-skin", {"fdid": int(sk[0])})
        for (a, v), data in anims.items():
            self.write(M.anim_name(rel, a, v), data, kind + "-anim", {"of": int(fdid)})
        if anims:
            report["anims"] = len(anims)
        c = RM.chunks(raw)
        if "PFDC" in c or "PFID" in c:
            report["physics"] = "retail runtime physics: shipped in its bind pose (3.3.5 has none)"
        self.man[rel.lower()]["src"] = report
        return report

    def external_anims(self, fdid: int, raw: bytes, body: bytes) -> dict:
        ext = [(s, a, v) for s, a, v in required_external_animations(body) if a <= MAX_ANIM_ID]
        if not ext:
            return {}
        c = RM.chunks(raw)
        mapping = {(a, v): f for a, v, f in RM.afid(c["AFID"][0])} if "AFID" in c else {}
        out = {}
        for _s, a, v in ext:
            f = mapping.get((a, v))
            if not f:
                raise RuntimeError("animation %d-%d of %d has no file in AFID" % (a, v, fdid))
            out[(a, v)] = animation_payload(self.files.read(f))
        return out

    def copy_anims(self, src_rel: str, rel: str) -> None:
        base = src_rel[:-3].lower()
        for k, v in list(self.man.items()):
            if k.startswith(base) and k.endswith(".anim"):
                dst = rel[:-3] + v["path"][len(src_rel) - 3:]
                if not self.have(dst):
                    self.write(dst, self.read_built(v["path"]), "anim", {"copy_of": v["path"]})


# ---------------------------------------------------------------------------
# helpers (pure functions on model bytes)
# ---------------------------------------------------------------------------
def texture_kinds(body: bytes) -> list[int]:
    n, o = struct.unpack_from("<II", body, MP.H_TEXTURES)
    return [struct.unpack_from("<I", body, o + i * 16)[0] for i in range(n)]


def align_txid(kinds, ids):
    """TXID names the file-backed (type 0) slots only, in order."""
    out, at = [], 0
    for k in kinds:
        if k == 0:
            out.append(int(ids[at]) if at < len(ids) else 0)
            at += 1
        else:
            out.append(None)
    return out


def without_new_particles(raw: bytes) -> bytes:
    """Global flag 0x200 = a newer emitter record m2_port refuses to guess:
    the model keeps its mesh and loses only those emitters."""
    _body, _version, chunks = MP.unwrap(raw)
    base = chunks[b"MD21"][0] if chunks else 0
    flags = struct.unpack_from("<I", raw, base + 0x10)[0]
    if not flags & 0x200:
        return raw
    b = bytearray(raw)
    struct.pack_into("<I", b, base + 0x10, flags & ~0x200)
    struct.pack_into("<II", b, base + MP.H_PARTICLES, 0, 0)
    return bytes(b)


def animation_payload(raw: bytes) -> bytes:
    """A 3.3.5 .anim holds the AFM2 payload without retail's chunk wrapper."""
    if raw[:4] not in (b"AFM2", b"AFSA", b"AFSB"):
        return raw
    c = RM.chunks(raw)
    return c["AFM2"][0] if "AFM2" in c else raw


def required_external_animations(body: bytes):
    """[(seq, animId, variation)] of external sequences with real keys
    (items often carry the character's whole sequence list with no keys in
    most slots - those need no file)."""
    result = []
    tracks = list(VB.TRACKS) + [("particle.enabled", "particles", "particle", 0x1C8)]
    for seq, aid, var in M.external_set(body):
        if aid > MAX_ANIM_ID:
            continue
        used = False
        for _label, array, kind, field in tracks:
            count, offset = struct.unpack_from("<II", body, VB.H[array])
            for i in range(count):
                track = offset + i * VB.SZ[kind] + field
                if struct.unpack_from("<h", body, track + 2)[0] >= 0:
                    continue
                for pair in (4, 12):
                    oc, outer = struct.unpack_from("<II", body, track + pair)
                    if seq < oc and struct.unpack_from("<I", body, outer + seq * 8)[0]:
                        used = True
                        break
                if used:
                    break
            if used:
                break
        if used:
            result.append((seq, aid, var))
    return result


def bake_exp2(raw: bytes, body: bytearray, report: dict) -> None:
    c = RM.chunks(raw)
    if "EXP2" not in c:
        return
    b = c["EXP2"][0]
    cnt, off = struct.unpack_from("<II", b, 0)
    n_p, o_p = struct.unpack_from("<II", body, MP.H_PARTICLES)
    for i in range(min(cnt, n_p)):
        _z, cm, am = struct.unpack_from("<3f", b, off + i * 28)
        rec = o_p + i * MP.SZ_PARTICLE_WOTLK
        if abs(am - 1.0) > 1e-4:
            for pos in MP._fblock_fixed16(body, rec + 0x114):
                v = struct.unpack_from("<h", body, pos)[0]
                struct.pack_into("<h", body, pos, int(round(max(0.0, min(32767.0, v * am)))))
            report.setdefault("exp2", []).append("emitter %d alpha x%.3g" % (i, am))
        if abs(cm - 1.0) > 1e-4:
            nv, ov = struct.unpack_from("<II", body, rec + 0x104 + 8)
            for k in range(nv * 3):
                v = struct.unpack_from("<f", body, ov + k * 4)[0]
                struct.pack_into("<f", body, ov + k * 4, min(255.0, v * cm))
            report.setdefault("exp2", []).append("emitter %d colour x%.3g" % (i, cm))


def bake_particle_colour(body: bytearray, row, pc_id: int, report: dict) -> None:
    if not row:
        report["particle_color_missing"] = pc_id
        return
    n_p, o_p = struct.unpack_from("<II", body, MP.H_PARTICLES)
    done = 0
    for i in range(n_p):
        rec = o_p + i * MP.SZ_PARTICLE_WOTLK
        idx = struct.unpack_from("<H", body, rec + 0x2A)[0]
        if idx not in (11, 12, 13):
            continue
        start, mid, end = row[idx - 11]
        nt, ot, nv, ov = struct.unpack_from("<IIII", body, rec + 0x104)
        times = [x / 32767.0 for x in struct.unpack_from("<%dH" % nt, body, ot)] if nt else []
        tmid = times[len(times) // 2] if len(times) >= 3 else 0.5
        tmid = min(max(tmid, 1e-3), 1 - 1e-3)
        for k in range(nv):
            t = times[k] if k < len(times) else (k / max(1, nv - 1))
            if t <= tmid:
                f = t / tmid
                col = [start[j] + (mid[j] - start[j]) * f for j in range(3)]
            else:
                f = (t - tmid) / (1 - tmid)
                col = [mid[j] + (end[j] - mid[j]) * f for j in range(3)]
            struct.pack_into("<3f", body, ov + k * 12, *[float(x) for x in col])
        struct.pack_into("<H", body, rec + 0x2A, 0)
        done += 1
    if done:
        report["particle_color"] = {"id": pc_id, "emitters": done}


def constant_colours(body: bytearray, report: dict) -> bytearray:
    n_seq, o_seq = struct.unpack_from("<II", body, MP.H_SEQUENCES)
    if n_seq < 2:
        return body
    inside = [bool(struct.unpack_from("<I", body, o_seq + i * 64 + 0x0C)[0] & 0x20) for i in range(n_seq)]
    n_c, o_c = struct.unpack_from("<II", body, MP.H_COLORS_A)
    filled = []
    for ci in range(n_c):
        for part, vsize in ((0x00, 12), (0x14, 2)):
            tr = o_c + ci * MP.SZ_COLOR_A + part
            _interp, gseq = struct.unpack_from("<hh", body, tr)
            if gseq >= 0:
                continue
            nt, ot = struct.unpack_from("<II", body, tr + 4)
            nv, ov = struct.unpack_from("<II", body, tr + 12)
            if nt != n_seq or nv != n_seq:
                continue
            keyed, vals = [], set()
            for s in range(n_seq):
                cnt, o = struct.unpack_from("<II", body, ov + s * 8)
                if cnt and inside[s]:
                    keyed.append(s)
                    for k in range(cnt):
                        vals.add(bytes(body[o + k * vsize:o + (k + 1) * vsize]))
            empty = [s for s in range(n_seq) if inside[s] and not struct.unpack_from("<I", body, ov + s * 8)[0]]
            if not keyed or not empty or len(vals) != 1:
                continue
            value = next(iter(vals))
            while len(body) % 16:
                body.append(0)
            t_at = len(body)
            body += struct.pack("<I", 0) + bytes(12)
            v_at = len(body)
            body += value + bytes(16 - vsize)
            for s in empty:
                struct.pack_into("<II", body, ot + s * 8, 1, t_at)
                struct.pack_into("<II", body, ov + s * 8, 1, v_at)
            filled.append("colour %d %s" % (ci, "rgb" if part == 0 else "alpha"))
    if filled:
        report["constant_colours"] = filled
    return body


def baked_tag(baked, particle_color: int = 0) -> str:
    """'' for a model drawn as retail ships it, else a short stable tag."""
    if not baked and not particle_color:
        return ""
    key = ",".join("%d:%d" % (k, baked[k]) for k in sorted(baked or {}))
    if particle_color:
        key += ";pc%d" % particle_color
    return "__" + hashlib.sha1(key.encode()).hexdigest()[:8]


def cli(prj, log, fdid: int, out: str | None) -> int:
    """Any retail model (a doodad, an effect, a creature part) -> 3.3.5."""
    b = Builder(prj)
    rel = out or "Item\\ObjectComponents\\%s\\%s.m2" % (prj.ns, b.stem(fdid))
    rep = b.convert_model(fdid, rel)
    b.save()
    log(json.dumps(rep, indent=1))
    log("written:", os.path.join(prj.build_dir, rel))
    return 0
