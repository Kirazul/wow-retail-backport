"""
mpq_read.py - read an MPQ the way the client does.

This exists to be the oracle for tools/test_mpq.py. Verifying our writer with a
reader that does not share the client's rules proves nothing, and mpyq does not
share them: it decides whether a sector is compressed by comparing the WHOLE
file's remaining length against one sector's length, so any raw sector in a
multi-sector file makes it try to inflate raw bytes.

The rule the client actually uses, per sector:

    expected = min(sector_size, bytes_remaining_in_file)
    stored   = next_offset - this_offset
    stored == expected  ->  the sector is raw, copy it
    stored <  expected  ->  first byte is a compression mask, inflate the rest

A reader is only worth trusting if it has been checked against archives nobody
here produced, so `--selftest` reads real Blizzard files out of the installed
client and requires byte-for-byte agreement with mpyq on every file mpyq can
read - which is exactly the set where mpyq is known to be right.
"""

from __future__ import annotations
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
import _toolpath  # noqa: E402,F401  (every tools/ folder and re/ on sys.path)

import os
import struct
import sys
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

FILE_COMPRESS = 0x00000200
FILE_IMPLODE = 0x00000100
FILE_ENCRYPTED = 0x00010000
FILE_SINGLE_UNIT = 0x01000000
FILE_SECTOR_CRC = 0x04000000
FILE_EXISTS = 0x80000000

HASH_EMPTY = 0xFFFFFFFF

from mpq_write import (HASH_FILE_KEY, HASH_NAME_A, HASH_NAME_B,  # noqa: E402
                       HASH_TABLE_OFFSET, CRYPT, hash_str)


def decrypt(data: bytes, key: int) -> bytes:
    n = len(data) // 4
    words = struct.unpack("<%dI" % n, data[: n * 4])
    seed = 0xEEEEEEEE
    out = []
    for v in words:
        seed = (seed + CRYPT[0x400 + (key & 0xFF)]) & 0xFFFFFFFF
        ch = (v ^ ((key + seed) & 0xFFFFFFFF)) & 0xFFFFFFFF
        key = (((~key << 0x15) & 0xFFFFFFFF) + 0x11111111) | (key >> 0x0B)
        key &= 0xFFFFFFFF
        seed = (ch + seed + (seed << 5) + 3) & 0xFFFFFFFF
        out.append(ch)
    return struct.pack("<%dI" % n, *out) + data[n * 4:]


def _inflate(sector: bytes) -> bytes:
    mask = sector[0]
    body = sector[1:]
    if mask & 0x02:
        return zlib.decompress(body)
    if mask == 0x10:
        import bz2
        return bz2.decompress(body)
    raise RuntimeError("unsupported compression mask 0x%02X" % mask)


class MpqReader:
    def __init__(self, path: str, fh=None):
        # fh: any seekable file-like object (tools/races/coa_fetch.py passes a
        # remote one that downloads only the byte ranges actually read).
        self.fh = fh if fh is not None else open(path, "rb")
        head = self.fh.read(32)
        if head[:4] != b"MPQ\x1a":
            raise ValueError("%s: not an MPQ" % path)
        (_m, _hs, self.asize, self.version, self.sector_shift,
         hpos, bpos, hsize, bsize) = struct.unpack("<4sIIHHIIII", head)
        self.sector_size = 512 << self.sector_shift

        self.fh.seek(hpos)
        hbuf = decrypt(self.fh.read(hsize * 16), hash_str("(hash table)", HASH_FILE_KEY))
        self.fh.seek(bpos)
        bbuf = decrypt(self.fh.read(bsize * 16), hash_str("(block table)", HASH_FILE_KEY))

        self.hash = [struct.unpack_from("<IIHHI", hbuf, i * 16) for i in range(hsize)]
        self.block = [struct.unpack_from("<IIII", bbuf, i * 16) for i in range(bsize)]
        self.hsize = hsize

    def _find(self, name: str):
        start = hash_str(name, HASH_TABLE_OFFSET) & (self.hsize - 1)
        a = hash_str(name, HASH_NAME_A)
        b = hash_str(name, HASH_NAME_B)
        i = start
        for _ in range(self.hsize):
            na, nb, _loc, _plat, blk = self.hash[i]
            if blk == HASH_EMPTY:
                return None
            if na == a and nb == b:
                return blk
            i = (i + 1) & (self.hsize - 1)
        return None

    def locate(self, name: str):
        """(offset, packed, size, flags) of an existing file, else None."""
        blk = self._find(name.replace("/", "\\"))
        if blk is None or blk >= len(self.block):
            return None
        entry = self.block[blk]
        return entry if entry[3] & FILE_EXISTS else None

    def read(self, name: str):
        blk = self._find(name.replace("/", "\\"))
        if blk is None or blk >= len(self.block):
            return None
        off, packed, size, flags = self.block[blk]
        if not (flags & FILE_EXISTS):
            return None
        if flags & FILE_ENCRYPTED:
            raise NotImplementedError("encrypted files are not supported")
        if size == 0:
            return b""

        self.fh.seek(off)
        raw = self.fh.read(packed)

        if not (flags & (FILE_COMPRESS | FILE_IMPLODE)):
            return raw[:size]                       # stored plain

        if flags & FILE_SINGLE_UNIT:
            return _inflate(raw) if packed < size else raw[:size]

        nsec = (size + self.sector_size - 1) // self.sector_size
        ncount = nsec + 1 + (1 if flags & FILE_SECTOR_CRC else 0)
        pos = struct.unpack_from("<%dI" % ncount, raw, 0)

        out = bytearray()
        left = size
        for i in range(nsec):
            sec = raw[pos[i]:pos[i + 1]]
            expect = min(self.sector_size, left)
            # THE RULE. Equal means the sector was stored raw because
            # compressing it did not help; smaller means it carries a
            # compression mask byte.
            out += sec if len(sec) == expect else _inflate(sec)
            left -= expect
        return bytes(out[:size])


def selftest(archive: str, limit: int = 40) -> int:
    """Check this reader against mpyq on an archive neither of us produced."""
    import mpyq
    a = mpyq.MPQArchive(archive)
    mine = MpqReader(archive)
    lf = a.read_file("(listfile)")
    if not lf:
        print("no listfile in %s" % archive)
        return 1
    names = [x for x in lf.decode("latin-1").replace("\r", "").split("\n") if x]
    names = names[:: max(1, len(names) // limit)][:limit]

    checked = skipped = bad = 0
    for n in names:
        try:
            ref = a.read_file(n)
        except Exception:
            skipped += 1                 # mpyq cannot read it; no opinion to compare
            continue
        if ref is None:
            skipped += 1
            continue
        got = mine.read(n)
        if got != ref:
            bad += 1
            print("  MISMATCH %-60s ref %s ours %s"
                  % (n, len(ref), "None" if got is None else len(got)))
        else:
            checked += 1
    print("%s: agreed on %d, mpyq declined %d, disagreed %d"
          % (os.path.basename(archive), checked, skipped, bad))
    return 1 if bad else 0


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", nargs="+", metavar="MPQ")
    ap.add_argument("--limit", type=int, default=40)
    g = ap.parse_args()
    if g.selftest:
        rc = 0
        for p in g.selftest:
            rc |= selftest(p, g.limit)
        raise SystemExit(rc)
    ap.print_help()
