"""
mpq_write.py - write an MPQ archive the 3.3.5a client will read.

WHY WRITE AN ARCHIVE RATHER THAN HOOK THE FILE SYSTEM
-----------------------------------------------------
A patch needs the client to load models it does not ship. The obvious move
is to hook the file layer and serve the bytes ourselves. Don't: in 3.3.5a model
and texture loading happens on worker threads, so that hook has to be re-entrant
and thread-safe forever, and every mistake in it is a crash in someone else's
stack. An archive is data. The client's own loader reads it, on its own threads,
with code Blizzard already debugged.

It is also the only form of this that is verifiable offline, which is the whole
reason the rest of this project has gates.

THE FORMAT, AS MUCH OF IT AS IS NEEDED
--------------------------------------
MPQ v1: a 32-byte header, a hash table, a block table, and the file data.

  hash table   2^n entries of (nameA, nameB, locale, platform, blockIndex).
               A path is located by hashing it three ways: one hash picks the
               starting slot, the other two confirm the entry is really that
               path. Collisions walk forward. Both tables are encrypted.
  block table  one (offset, packedSize, size, flags) per file.
  file data    split into sectors; a leading table of sector offsets, then each
               sector compressed independently.

A sector that does not get smaller is stored raw, and StormLib recognises that
by size alone - so the compression byte is present only on sectors that were
actually compressed. Getting that backwards produces an archive that opens,
lists correctly, and returns corrupt bytes.
"""

from __future__ import annotations
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
import _toolpath  # noqa: E402,F401  (puts scripts/lib on sys.path)

import struct
import zlib

# --- Storm's crypt table ---------------------------------------------------
def _crypt_table():
    t = [0] * 0x500
    seed = 0x00100001
    for i in range(0x100):
        idx = i
        for _ in range(5):
            seed = (seed * 125 + 3) % 0x2AAAAB
            a = (seed & 0xFFFF) << 16
            seed = (seed * 125 + 3) % 0x2AAAAB
            b = seed & 0xFFFF
            t[idx] = a | b
            idx += 0x100
    return t


CRYPT = _crypt_table()

HASH_TABLE_OFFSET, HASH_NAME_A, HASH_NAME_B, HASH_FILE_KEY = 0, 1, 2, 3


def hash_str(s: str, kind: int) -> int:
    """Storm's string hash. Paths are upper-cased and backslash-separated."""
    s = s.replace("/", "\\").upper()
    seed1, seed2 = 0x7FED7FED, 0xEEEEEEEE
    for c in s:
        ch = ord(c)
        seed1 = (CRYPT[(kind << 8) + ch] ^ (seed1 + seed2)) & 0xFFFFFFFF
        seed2 = (ch + seed1 + seed2 + (seed2 << 5) + 3) & 0xFFFFFFFF
    return seed1


def encrypt(data: bytes, key: int) -> bytes:
    n = len(data) // 4
    words = list(struct.unpack("<%dI" % n, data[: n * 4]))
    seed = 0xEEEEEEEE
    out = []
    for v in words:
        seed = (seed + CRYPT[0x400 + (key & 0xFF)]) & 0xFFFFFFFF
        ch = (v ^ ((key + seed) & 0xFFFFFFFF)) & 0xFFFFFFFF
        key = (((~key << 0x15) & 0xFFFFFFFF) + 0x11111111) | (key >> 0x0B)
        key &= 0xFFFFFFFF
        seed = (v + seed + (seed << 5) + 3) & 0xFFFFFFFF
        out.append(ch)
    return struct.pack("<%dI" % n, *out) + data[n * 4:]


# --- flags -----------------------------------------------------------------
FILE_IMPLODE = 0x00000100
FILE_COMPRESS = 0x00000200
FILE_ENCRYPTED = 0x00010000
FILE_SINGLE_UNIT = 0x01000000
FILE_EXISTS = 0x80000000

COMP_ZLIB = 0x02

HASH_ENTRY_EMPTY = 0xFFFFFFFF


class MpqWriter:
    """
    Build an archive in one pass.

    Files are appended as they arrive, so the data region is written straight
    to disk and never held in memory - which matters when the payload is
    gigabytes of art.
    """

    def __init__(self, path: str, sector_shift: int = 3):
        self.path = path
        self.sector_shift = sector_shift
        self.sector_size = 512 << sector_shift
        # WRITTEN BESIDE THE TARGET, MOVED ONTO IT AT THE END.
        #
        # The header is the last thing written, so a run that is still going -
        # or that died - leaves a file that is not a readable archive. Writing
        # straight to the destination therefore publishes a broken archive for
        # the whole duration of the build, and if the client is started in that
        # window it loads models with no textures and draws the missing-texture
        # checkerboard. Ask how I know.
        #
        # os.replace is atomic on the same volume, so the destination only ever
        # holds a complete archive or the previous one.
        self.tmp = path + ".building"
        self.fh = open(self.tmp, "wb")
        self.fh.write(b"\x00" * 32)          # header written last
        self.files = []                       # (name, offset, packed, size, flags)
        # IDENTICAL CONTENT IS STORED ONCE. A hash entry names a block, and a
        # block is only (offset, packed, size, flags): with no encryption the
        # bytes do not depend on the name, so any number of names may point at
        # one stored copy. The race pack carries the same helm cut, skin and
        # texture under many names (colour variants, shared retail files):
        # ~100 MB of it before compression.
        self._seen = {}

    # -- one file ----------------------------------------------------------
    def add(self, name: str, data: bytes, level: int = 6) -> None:
        """
        Store one file, either fully compressed or not compressed at all.

        A compressed MPQ file may legally hold a RAW sector, and Blizzard's own
        archives are full of them: the reader notices that a sector's stored
        length equals its uncompressed length and copies it instead of
        inflating it. That is the shape the client reads every day, so it is
        the shape written here.

        It is NOT the shape mpyq reads correctly - mpyq compares the whole
        file's remaining length against one sector's, so a raw sector in a
        multi-sector file makes it try to inflate raw bytes. That is a fact
        about mpyq, not about the format, which is why the round-trip test uses
        the reader in mpq_read.py instead - and why that reader is itself
        checked against Blizzard's shipped archives before it is trusted.
        """
        name = name.replace("/", "\\")
        import hashlib
        key = hashlib.sha1(data).digest() + len(data).to_bytes(8, "little")
        if key in self._seen:
            self.files.append((name,) + self._seen[key])
            return
        offset = self.fh.tell()
        size = len(data)

        if size == 0:
            # A zero-length file still needs a block entry or the path resolves
            # to nothing; it just has no sectors.
            self.files.append((name, offset, 0, 0, FILE_EXISTS))
            return

        nsec = (size + self.sector_size - 1) // self.sector_size
        sectors = []
        for i in range(nsec):
            raw = data[i * self.sector_size:(i + 1) * self.sector_size]
            packed = bytes([COMP_ZLIB]) + zlib.compress(raw, level)
            # Strictly smaller, or store it raw. Equal-or-larger stored as
            # "compressed" is the one encoding the reader cannot tell apart
            # from a raw sector, so it must never be produced.
            sectors.append(packed if len(packed) < len(raw) else raw)

        table = [0] * (nsec + 1)
        pos = (nsec + 1) * 4
        for i, s in enumerate(sectors):
            table[i] = pos
            pos += len(s)
        table[nsec] = pos

        self.fh.write(struct.pack("<%dI" % (nsec + 1), *table))
        for s in sectors:
            self.fh.write(s)

        self.files.append((name, offset, pos, size, FILE_EXISTS | FILE_COMPRESS))
        self._seen[key] = (offset, pos, size, FILE_EXISTS | FILE_COMPRESS)

    # -- finish ------------------------------------------------------------
    def close(self, listfile: bool = True) -> None:
        if listfile:
            names = "\r\n".join(n for n, *_ in self.files) + "\r\n"
            self.add("(listfile)", names.encode("latin-1", "replace"))

        n = len(self.files)
        hsize = 4
        while hsize < n * 2:            # keep the table at most half full
            hsize <<= 1
        hsize = max(hsize, 0x1000)

        # hash table
        empty = struct.pack("<IIHHI", 0xFFFFFFFF, 0xFFFFFFFF, 0xFFFF, 0xFFFF,
                            HASH_ENTRY_EMPTY)
        table = [None] * hsize
        for idx, (name, *_rest) in enumerate(self.files):
            start = hash_str(name, HASH_TABLE_OFFSET) & (hsize - 1)
            a = hash_str(name, HASH_NAME_A)
            b = hash_str(name, HASH_NAME_B)
            i = start
            while table[i] is not None:
                i = (i + 1) & (hsize - 1)
                if i == start:
                    raise RuntimeError("hash table full")
            table[i] = struct.pack("<IIHHI", a, b, 0, 0, idx)

        hbuf = b"".join(e if e is not None else empty for e in table)
        bbuf = b"".join(struct.pack("<IIII", off, packed, size, flags)
                        for _n, off, packed, size, flags in self.files)

        hpos = self.fh.tell()
        self.fh.write(encrypt(hbuf, hash_str("(hash table)", HASH_FILE_KEY)))
        bpos = self.fh.tell()
        self.fh.write(encrypt(bbuf, hash_str("(block table)", HASH_FILE_KEY)))
        asize = self.fh.tell()

        self.fh.seek(0)
        self.fh.write(struct.pack("<4sIIHHIIII",
                                  b"MPQ\x1a", 32, asize, 0, self.sector_shift,
                                  hpos, bpos, hsize, n))
        self.fh.close()
        # Only now is it an archive. Publish it in one step.
        import os
        os.replace(self.tmp, self.path)
