"""
blp.py - BLP2 textures in and out, as numpy RGBA.

    img = blp.decode(data)            # (h, w, 4) uint8, mip 0
    data = blp.encode(img, "dxt5")    # BLP2 with a full mip chain

Decoding goes through Pillow's own BLP reader (palettised, DXT1/3/5, raw), so
every retail and client texture reads the same way the rest of the pipeline
reads it. Encoding writes the format the 3.3.5a client reads (BLP2, encoding 2
= DXT, preferredFormat 0/1/7 for DXT1/3/5) with every mip level down to 1x1:
the block payload of each level comes from Pillow's DDS encoder, and only the
container is ours.
"""

from __future__ import annotations
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
import _toolpath  # noqa: E402,F401  (every tools/ folder and re/ on sys.path)

import io
import struct

import numpy as np
from PIL import Image

_PREF = {"dxt1": 0, "dxt3": 1, "dxt5": 7}
_ALPHA_BITS = {"dxt1": 1, "dxt3": 8, "dxt5": 8}


def decode(data: bytes) -> np.ndarray:
    """mip 0 of a BLP as an (h, w, 4) uint8 RGBA array.

    Palettised BLP2 (encoding 1) is decoded here, not by Pillow: Pillow 12.3
    takes the alpha from the palette (always 0 in Blizzard's files) and drops
    the alpha plane that follows the indices, so every palettised texture with
    alpha came out fully transparent (retail Pandaren female underwear,
    measured: 29-45% of texels opaque in the file, 0% after Pillow). An alpha
    depth of 0 means opaque, whatever the payload says."""
    if data[:4] == b"BLP2" and data[8] == 1:
        return _decode_palette(data)
    im = Image.open(io.BytesIO(data))
    im.load()
    out = np.asarray(im.convert("RGBA"), dtype=np.uint8).copy()
    if data[:4] == b"BLP2" and data[9] == 0:
        out[..., 3] = 255
    return out


def _decode_palette(data: bytes) -> np.ndarray:
    """BLP2 encoding 1: 256 BGRA palette entries, w*h indices, then the alpha
    plane at the header's depth (1 bit LSB-first, 4 bits low nibble first, or
    8 bits)."""
    depth = data[9]
    w, h = struct.unpack_from("<II", data, 12)
    off = struct.unpack_from("<I", data, 20)[0]
    n = w * h
    pal = np.frombuffer(data, np.uint8, 1024, 148).reshape(256, 4)
    idx = np.frombuffer(data, np.uint8, n, off)
    rgb = pal[idx][:, [2, 1, 0]]
    if depth == 0:
        a = np.full(n, 255, np.uint8)
    elif depth == 8:
        a = np.frombuffer(data, np.uint8, n, off + n).copy()
    elif depth == 4:
        b = np.frombuffer(data, np.uint8, (n + 1) // 2, off + n)
        a = (np.stack([b & 0x0F, b >> 4], axis=1).reshape(-1)[:n] * 17).astype(np.uint8)
    elif depth == 1:
        b = np.frombuffer(data, np.uint8, (n + 7) // 8, off + n)
        a = (np.unpackbits(b, bitorder="little")[:n] * 255).astype(np.uint8)
    else:
        raise ValueError("palettised BLP with alpha depth %d" % depth)
    return np.concatenate([rgb, a[:, None]], axis=1).reshape(h, w, 4).copy()


def levels(data: bytes) -> list[tuple[int, int]]:
    """(w, h) of every mip level the BLP2 stores, level 0 first."""
    if data[:4] != b"BLP2":
        raise ValueError("not a BLP2")
    w, h = struct.unpack_from("<II", data, 12)
    offs = struct.unpack_from("<16I", data, 20)
    sizes = struct.unpack_from("<16I", data, 84)
    out = []
    for i in range(16):
        if not offs[i] or not sizes[i] or offs[i] + sizes[i] > len(data):
            break
        out.append((max(1, w >> i), max(1, h >> i)))
        if not data[11] and i == 0:            # no mips: level 0 only
            break
    return out


def level_for(data: bytes, w: int, h: int) -> int:
    """The level stored at exactly (w, h) - the texture's own downscale, made
    by its author's tools - or 0 when no level has that size."""
    for i, s in enumerate(levels(data)):
        if s == (w, h):
            return i
    return 0


def level_only(data: bytes, level: int = 0) -> bytes:
    """The BLP2 with only `level`, as its level 0: same encoding, same bytes."""
    if data[:4] != b"BLP2" or data[8] not in (1, 2, 3):
        raise ValueError("not a BLP2 the compositor reads")
    w, h = levels(data)[level]
    offs = struct.unpack_from("<16I", data, 20)
    sizes = struct.unpack_from("<16I", data, 84)
    head = bytearray(data[:148 + 1024])
    head[11] = 0                              # hasMips
    struct.pack_into("<II", head, 12, w, h)
    body = data[offs[level]:offs[level] + sizes[level]]
    struct.pack_into("<16I", head, 20, len(head), *([0] * 15))
    struct.pack_into("<16I", head, 84, len(body), *([0] * 15))
    return bytes(head) + body


def size(data: bytes) -> tuple[int, int]:
    """(width, height) from the header alone."""
    if data[:4] != b"BLP2":
        raise ValueError("not a BLP2")
    return struct.unpack_from("<II", data, 12)


def _mips(img: np.ndarray) -> list[np.ndarray]:
    """The chain the client samples, by 2x2 averaging with alpha-weighted
    colour so transparent texels never bleed their colour into the next level."""
    out = [img]
    cur = img.astype(np.float64)
    while cur.shape[0] > 1 or cur.shape[1] > 1:
        h, w = cur.shape[:2]
        h2, w2 = max(1, h // 2), max(1, w // 2)
        if h == 1:
            c = cur.reshape(1, w2, 2, 4)
            blocks = [c[:, :, 0], c[:, :, 1]]
        elif w == 1:
            c = cur.reshape(h2, 2, 1, 4)
            blocks = [c[:, 0], c[:, 1]]
        else:
            c = cur.reshape(h2, 2, w2, 2, 4)
            blocks = [c[:, 0, :, 0], c[:, 0, :, 1], c[:, 1, :, 0], c[:, 1, :, 1]]
        a = sum(b[..., 3] for b in blocks)
        rgb = sum(b[..., :3] * b[..., 3:4] for b in blocks)
        plain = sum(b[..., :3] for b in blocks) / len(blocks)
        with np.errstate(invalid="ignore", divide="ignore"):
            col = np.where(a[..., None] > 0, rgb / np.maximum(a[..., None], 1e-9), plain)
        nxt = np.concatenate([col, (a / len(blocks))[..., None]], axis=-1)
        out.append(np.clip(np.rint(nxt), 0, 255).astype(np.uint8))
        cur = nxt
    return out


def _blocks(img: np.ndarray, fmt: str) -> bytes:
    """One level's DXT payload, from Pillow's DDS encoder (header stripped)."""
    h, w = img.shape[:2]
    # DXT needs whole 4x4 blocks; the client reads a 2x2 or 1x1 level as one
    # block, so pad by edge replication and keep the block.
    ph, pw = max(4, (h + 3) // 4 * 4), max(4, (w + 3) // 4 * 4)
    if (ph, pw) != (h, w):
        img = np.pad(img, ((0, ph - h), (0, pw - w), (0, 0)), mode="edge")
    b = io.BytesIO()
    Image.fromarray(img, "RGBA").save(b, "DDS", pixel_format=fmt.upper())
    raw = b.getvalue()
    hdr = 128
    if raw[84:88] == b"DX10":
        hdr += 20
    need = (pw // 4) * (ph // 4) * (8 if fmt == "dxt1" else 16)
    payload = raw[hdr:hdr + need]
    if len(payload) != need:
        raise RuntimeError("DDS payload %d, expected %d" % (len(payload), need))
    return payload


def encode(img: np.ndarray, fmt: str = "dxt5") -> bytes:
    """An (h, w, 4) uint8 RGBA array -> BLP2 bytes, DXT, full mip chain.
    Width and height must be powers of two (the client's own rule)."""
    fmt = fmt.lower()
    h, w = img.shape[:2]
    if w & (w - 1) or h & (h - 1):
        raise ValueError("BLP sides must be powers of two, got %dx%d" % (w, h))
    if fmt == "dxt1" and (img[..., 3] < 255).any():
        # punch-through only: anything not opaque becomes fully transparent
        img = img.copy()
        img[..., 3] = np.where(img[..., 3] >= 128, 255, 0).astype(np.uint8)
    levels = _mips(np.ascontiguousarray(img, dtype=np.uint8))[:16]
    payloads = [_blocks(l, fmt) for l in levels]
    head = bytearray(148)
    head[0:4] = b"BLP2"
    struct.pack_into("<IBBBB", head, 4, 1, 2, _ALPHA_BITS[fmt], _PREF[fmt], 1)
    struct.pack_into("<II", head, 12, w, h)
    offs, sizes, pos = [0] * 16, [0] * 16, 148 + 1024
    for i, p in enumerate(payloads):
        offs[i], sizes[i] = pos, len(p)
        pos += len(p)
    struct.pack_into("<16I", head, 20, *offs)
    struct.pack_into("<16I", head, 84, *sizes)
    return bytes(head) + bytes(1024) + b"".join(payloads)


def encode_palette(img: np.ndarray) -> bytes:
    """An (h, w, 4) uint8 RGBA array -> BLP2 encoding 1 (256-colour palette,
    8-bit alpha when any texel is not opaque), full mip chain. The format of
    EVERY texture the 3.3.5a client composites onto a character (CharSections
    skins, faces, scalps; item TextureComponents): the compositor does not
    take DXT, a DXT section texture draws as the missing-texture green."""
    h, w = img.shape[:2]
    if w & (w - 1) or h & (h - 1):
        raise ValueError("BLP sides must be powers of two, got %dx%d" % (w, h))
    img = np.ascontiguousarray(img, dtype=np.uint8)
    alpha = bool((img[..., 3] < 255).any())
    levels = _mips(img)[:16]
    pal_img = Image.fromarray(levels[0][..., :3], "RGB").quantize(
        256, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE)
    pal = pal_img.getpalette()[:768]
    pal += [0] * (768 - len(pal))
    payloads = []
    for lv in levels:
        q = Image.fromarray(lv[..., :3], "RGB").quantize(
            palette=pal_img, dither=Image.Dither.FLOYDSTEINBERG)
        p = np.asarray(q, dtype=np.uint8).tobytes()
        if alpha:
            p += lv[..., 3].tobytes()
        payloads.append(p)
    head = bytearray(148)
    head[0:4] = b"BLP2"
    struct.pack_into("<IBBBB", head, 4, 1, 1, 8 if alpha else 0, 8, 1)
    struct.pack_into("<II", head, 12, w, h)
    offs, sizes, pos = [0] * 16, [0] * 16, 148 + 1024
    for i, p in enumerate(payloads):
        offs[i], sizes[i] = pos, len(p)
        pos += len(p)
    struct.pack_into("<16I", head, 20, *offs)
    struct.pack_into("<16I", head, 84, *sizes)
    bgra = bytearray(1024)
    for i in range(256):
        r, g, b = pal[3 * i:3 * i + 3]
        bgra[4 * i:4 * i + 4] = bytes((b, g, r, 255))
    return bytes(head) + bytes(bgra) + b"".join(payloads)


def resize(img: np.ndarray, w: int, h: int) -> np.ndarray:
    """Premultiplied Lanczos resample, so transparent texels do not darken the
    edges they border."""
    if img.shape[1] == w and img.shape[0] == h:
        return img
    f = img.astype(np.float32) / 255.0
    pm = f.copy()
    pm[..., :3] *= pm[..., 3:4]
    chans = [np.asarray(Image.fromarray(pm[..., c], "F").resize((w, h), Image.LANCZOS))
             for c in range(4)]
    out = np.stack(chans, axis=-1)
    a = np.clip(out[..., 3:4], 0.0, 1.0)
    rgb = np.where(a > 1e-6, out[..., :3] / np.maximum(a, 1e-6), 0.0)
    res = np.concatenate([np.clip(rgb, 0, 1), a], axis=-1)
    return np.clip(np.rint(res * 255.0), 0, 255).astype(np.uint8)
