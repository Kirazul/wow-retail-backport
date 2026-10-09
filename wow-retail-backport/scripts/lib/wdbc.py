"""
wdbc.py - 3.3.5a client tables (WDBC): read, append rows, write.

    t = Wdbc(data)                 # bytes of a .dbc
    t.rows                         # list of lists of uint32 (one per field)
    t.string(off)                  # a string field's text
    t.add([..], strings={col: "text"})   # append one row
    out = t.to_bytes()             # header + rows + string block

WDBC is flat: "WDBC", records, fields, record size, string size; then every
record as `fields` 32-bit values; then one string block that string fields
index (offset 0 = ""). Floats are stored bitwise, so a value round-trips
whatever its type. A table whose record size is not fields*4 (a packed custom
layout) is refused rather than guessed.
"""
from __future__ import annotations

import struct


class Wdbc:
    def __init__(self, data: bytes):
        if not data or data[:4] != b"WDBC":
            raise ValueError("not a WDBC table")
        n, f, rs, ss = struct.unpack_from("<4I", data, 4)
        if rs != f * 4:
            raise ValueError("record size %d != %d fields * 4 (packed layout: not supported)" % (rs, f))
        self.fields = f
        base = 20
        self.rows = [list(struct.unpack_from("<%dI" % f, data, base + i * rs)) for i in range(n)]
        sb = base + n * rs
        self.strings = bytearray(data[sb:sb + ss]) or bytearray(b"\0")
        self._index = None

    def string(self, off: int) -> str:
        if off <= 0 or off >= len(self.strings):
            return ""
        e = self.strings.index(b"\0", off)
        return self.strings[off:e].decode("utf-8", "replace")

    def add_string(self, s: str) -> int:
        if not s:
            return 0
        if self._index is None:
            self._index = {}
            o = 1
            while o < len(self.strings):
                e = self.strings.index(b"\0", o)
                self._index.setdefault(bytes(self.strings[o:e]), o)
                o = e + 1
        b = s.encode("utf-8")
        o = self._index.get(b)
        if o is None:
            o = len(self.strings)
            self.strings += b + b"\0"
            self._index[b] = o
        return o

    def ids(self) -> set:
        return {r[0] for r in self.rows}

    def max_id(self) -> int:
        return max((r[0] for r in self.rows), default=0)

    def by_id(self) -> dict:
        return {r[0]: r for r in self.rows}

    def add(self, values, strings=None):
        """Append one row. `values`: ints/floats per field (strings passed by
        column in `strings`). Refuses an id the table already has."""
        if len(values) != self.fields:
            raise ValueError("row has %d fields, table has %d" % (len(values), self.fields))
        row = []
        for v in values:
            if isinstance(v, float):
                row.append(struct.unpack("<I", struct.pack("<f", v))[0])
            else:
                row.append(int(v) & 0xFFFFFFFF)
        for col, s in (strings or {}).items():
            row[col] = self.add_string(s)
        if row[0] in self.ids():
            raise ValueError("id %d already exists in this table" % row[0])
        self.rows.append(row)

    def to_bytes(self) -> bytes:
        rows = sorted(self.rows, key=lambda r: r[0])
        out = bytearray(struct.pack("<4s4I", b"WDBC", len(rows), self.fields, self.fields * 4, len(self.strings)))
        for r in rows:
            out += struct.pack("<%dI" % self.fields, *r)
        out += self.strings
        return bytes(out)

    def find_string_field(self, row_id: int, text: str) -> int:
        """The column of row `row_id` whose string is `text` (case-insensitive),
        or -1. Used to locate a column by a value the table is known to hold
        (e.g. ChrRaces row 1 holds the prefix "Hu") instead of trusting a
        layout written down elsewhere."""
        r = self.by_id().get(row_id)
        if not r:
            return -1
        for i, v in enumerate(r):
            if 0 < v < len(self.strings) and self.string(v).lower() == text.lower():
                return i
        return -1
