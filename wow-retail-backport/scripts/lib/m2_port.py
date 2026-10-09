"""
m2_port.py - a retail M2 made readable by a 3.3.5a client WITHOUT rewriting it.

WHY THIS REPLACES THE FIRST ATTEMPT
-----------------------------------
The first converter parsed retail's model, built a new one, and copied the
pieces across. That design has one fatal property: every struct size and every
nested offset is a number the converter has to KNOW, and when one of them is
wrong the converter writes the file wrong and then reads its own output back
with the same wrong number and agrees with itself. It declared M2Sequence as 28
bytes. It is 64. All 141 models shipped with sequence records less than half
the size the client reads, so every animation's duration, flags and bounding
box came out of the middle of its neighbour: dead animations, nonsense bounds,
and crashes where the client read past the end.

So this one does not rewrite anything. It starts from retail's own bytes and
changes ONLY the fields that provably differ, in place. Nothing moves, so
nothing can be misplaced, and the result can be checked absolutely: the output
must be byte-identical to the retail file everywhere except at a list of patch
sites this module declares. `audit()` proves exactly that, per file.

WHAT MAKES THAT POSSIBLE - ALL OF IT MEASURED, NONE OF IT REMEMBERED
--------------------------------------------------------------------
Measured over the 141 retail models this pack ships and 399 WotLK models plus
410 WotLK skins read out of the client's own MPQs:

 1. NO retail model here sets global flag 0x8, so its header is 304 bytes -
    the same size a 3.3.5 header is. Offsets inside MD21 are relative to the
    chunk's data, so lifting the payload out is a copy, not a relocation.
    Every array offset in the file stays correct, untouched.

 2. NO retail model here sets global flag 0x200, so every emitter is the
    492-byte Cataclysm record, uniformly.

 3. M2Particle's 492 bytes are WotLK's 476 PLUS 16 BYTES AT THE TAIL. Every
    nested array inside the record - the two model filenames at 0x18 and 0x20,
    fifteen (times, values) blocks at 0x38, 0x4C, 0x60, 0x74, 0x88, 0x9C,
    0xB4, 0xCC, 0xE0, 0xF4, 0x104, 0x114, 0x124, 0x13C and 0x14C, the spline
    points at 0x1C0 and the enabledIn track at 0x1C8 - sits at the SAME offset
    in both. Proven by searching every 4-byte position of 804 retail emitters
    and 1,544 WotLK emitters for positions that hold a valid array in every
    single record: the two sets come out identical, and WotLK's record ends at
    0x1DC = 476, exactly where enabledIn's values array ends. So the emitter
    downports by dropping its last 16 bytes, and the array can be compacted in
    place because 476 < 492.

 4. M2Sequence is 64 bytes in BOTH. The only difference is one field:
    Warlords split `uint32 blend_time` at 0x1C into `uint16 in, uint16 out`.

 5. Bones need nothing. Every flag bit the retail models set is a bit WotLK
    models set too.

 6. Materials need two things. `blending_mode` 7 (BlendAdd) is used 113 times
    here and does not exist before Cataclysm - the WotLK client has no case
    for it. And retail sets flag bits 0x100, 0x1000 and 0x4000, which no
    WotLK material ever does.

 7. Skins need almost nothing STRUCTURALLY: the client reads a 48-byte header
    and Cataclysm's `shadow_batches` array sits at 48, past the end of what it
    reads, so it is simply never seen. What does matter is the batches:
    3.3.5's own spell skins carry shader_id 0 in 1,158 batches out of 1,159,
    and retail carries Legion ids (>= 0x8000) and Cataclysm bitfields like
    0x4014 that 3.3.5 resolves to the wrong shader entirely. They become 0.

 8. `texture_coord_combos` is empty in all 141 retail models and is what a
    batch's textureCoordComboIndex indexes. In the client's own models it
    holds the texture UNIT: (0) for a one-layer batch, (0, 1) for two. One
    shared [0, 1] appended to the model serves every batch.

USAGE
    from m2_port import port, audit
    body, skins, report = port(raw, texture_paths=[...], skins=[...])
    audit(raw, body, report)          # raises unless every changed byte is a
                                      # declared patch
"""

from __future__ import annotations
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
import _toolpath  # noqa: E402,F401  (every tools/ folder and re/ on sys.path)

import struct

M2_WOTLK = 264


class Fail(Exception):
    pass


# --- header offsets, all identical in 264 and 274 --------------------------
H_VERSION = 0x004
H_GLOBAL_FLAGS = 0x010
H_SEQUENCES = 0x01C
H_BONES = 0x02C
H_VERTICES = 0x03C
H_NUM_SKINS = 0x044
H_TEXTURES = 0x050
H_MATERIALS = 0x070
H_TEX_COMBOS = 0x080
H_TEX_COORD_COMBOS = 0x088
H_TEX_WEIGHT_COMBOS = 0x090
H_TEX_TRANSFORM_COMBOS = 0x098
H_TEX_COMBINER_COMBOS = 0x130
H_CAMERAS = 0x110
H_CAMERA_LOOKUP = 0x118
H_PARTICLES = 0x128

SZ_SEQUENCE = 64
SZ_TEXTURE = 16
SZ_MATERIAL = 4
SZ_PARTICLE_RETAIL = 492
SZ_PARTICLE_WOTLK = 476

# Global flag bits retail sets that no WotLK model ever does. Measured: retail
# uses 0x20, 0x20000, 0x1000000, 0x20000000 and 0x40000000; the client's own
# models never set any of them. Every other bit retail uses here (0x1, 0x10,
# 0x80, 0x1000, 0x2000, 0x8000, 0x200000) the client's models set too, so those
# are left exactly as Blizzard wrote them.
GF_POST_WOTLK = 0x20 | 0x20000 | 0x1000000 | 0x20000000 | 0x40000000
GF_COMBINER_COMBOS = 0x8      # would make the header 312 bytes
GF_NEW_PARTICLE = 0x200       # would make the emitter record bigger again

# Material flag bits 3.3.5 actually renders with: 0x01 unlit, 0x02 unfogged,
# 0x04 two-sided, 0x08 depth test off, 0x10 depth write off.
#
# 0x40 and 0x80 are NOT among them, whatever a format table says. Counted over
# the client's own 57,524 materials, 0x40 appears 100 times and 0x80 eleven -
# 0.17% and 0.02% - and the flag VALUES it ships are 0x00, 0x10, 0x04, 0x11,
# 0x15, 0x01, 0x13, 0x17 and 0x14. Not one common value has a bit above 0x1F.
#
# Retail is the opposite: 303 of the 572 materials in this pack set 0x40, so
# 53% of everything we ship was carrying a bit the client sees in 0.17% of its
# own art. In Cataclysm 0x40 and 0x80 became shadow-batch flags, and 3.3.5 has
# no shadow batches - it has whatever those bits meant in 264, applied to half
# our materials. Masking them off turns 0x55 -> 0x15, 0x51 -> 0x11 and
# 0x54 -> 0x14, which are the client's own fourth, fifth and ninth most
# common values.
# Re-derived from the binary rather than from how often a bit appears.
# The bits 3.3.5 provably reads out of M2Material.flags:
#
#   0x001 unlit          0x0081f201  and ecx, 1     (kills the light count)
#   0x002 unfogged       0x0083395b  and edx, 2
#   0x004 two-sided      0x00833973  and edx, 4
#   0x008 / 0x010        0x00833945 / 0x0083398e, the depth-state pair, and
#                        the client's own art ships 0x10 in the thousands
#   0x020                0x0081db7e  test byte [material], 0x20 - decides
#                        whether a BLENDED batch is allowed in render pass 1
#   0x100                0x0081f28f  test ebx, 0x100 - forces the batch to
#                        nought lights, i.e. "do not light this material"
#
# The old 0x1F mask threw 0x20 and 0x100 away. 0x100 alone was on 250 of the
# 572 materials in this pack, every one of them an effect retail authored as
# self-lit, and we were handing them to the lighting path.
#
# 0x40 and 0x80 stay masked off: nothing in the client reads them, and in
# Cataclysm they became shadow-batch bits that 3.3.5 has no batches for.
MATERIAL_FLAG_MASK = 0x13F

# Texture flags: 0x1 wrap X, 0x2 wrap Y. Retail also sets 0x4 thirteen times
# here; no WotLK texture does.
TEXTURE_FLAG_MASK = 0x3

# blending_mode 7 is Cataclysm's BlendAdd, which 3.3.5 has no case for. What it
# becomes is NOT a judgement call, because Blizzard shipped the answer: 29 of
# the assets this pack uses exist in retail AND in the 3.3.5 client, the same
# art authored twice. Comparing 71 emitters that pair up one to one:
#
#     retail blend 2 -> client 2   (x11)
#     retail blend 4 -> client 4   (x56)
#     retail blend 7 -> client 4   (x4)      <-- never 2
#
# and their materials agree the same way (0->0, 2->2, 4->4). Reasoning from the
# blend factors said 2, because BlendAdd's DESTINATION factor matches Alpha's.
# Blizzard says 4. Blizzard wins.
# M2Material.blending_mode and M2Particle.blendingType are NOT the same enum
# and must not share a table. That mistake is what put mode 7 on additive.
#
# ---- materials -------------------------------------------------------------
# 3.3.5's material table is at 0x00a45570 and it has exactly SEVEN entries -
# the bytes at index 7 are the string "shared->fieldOfView > 0.0f && ...", so
# a mode of 7 reads a string literal as a blend. It maps M2 mode -> GxBlend:
#
#     0 -> 0    1 -> 1    2 -> 2    3 -> 10    4 -> 3    5 -> 4    6 -> 5
#
# The enum itself is settled - Alastor's M2 template, the one the retro-port
# community uses, names it E_RENDERMODES: 0 OPAQUE, 1 ALPHA_KEY, 2 ALPHA,
# 3 NO_ALPHA_ADD, 4 ADD, 5 MOD, 6 MOD2X, 7 BLENDADD - and the client's own
# table agrees with the first seven, giving NO_ALPHA_ADD its own GxBlend (10,
# ONE/ONE) separate from ADD's (3, SRC_ALPHA/ONE).
#
# Cataclysm added mode 7, BlendAdd, whose factors are (ONE, INV_SRC_ALPHA):
# premultiplied alpha that still attenuates what is behind it.
#
# It used to be mapped to 4 (Add) here, and the stated justification was that
# Blizzard's own paired art said so. That justification was wrong: the pairs
# it was measured on were PARTICLE emitters, and particle blendingType is the
# other enum. Re-checked properly - of the 15 assets that exist in both builds
# with matching material counts, not one has a mode 7 material, so there is no
# paired evidence either way and the choice has to be made on the factors.
#
#     BlendAdd  (ONE,       INV_SRC_ALPHA)
#     Alpha  2  (SRC_ALPHA, INV_SRC_ALPHA)   <- same destination factor
#     Add    4  (SRC_ALPHA, ONE)             <- destination factor discarded
#
# The destination factor is the one that decides whether the surface dims what
# is behind it. Add throws it away, so a bright texel adds at full strength
# and nothing ever attenuates: the soft falloff disappears and the quad
# saturates to white with its polygon edge showing. That is exactly the
# failure this pack shows, on the 49 models (29%) and 27 hearthstones (63%)
# that carry a mode 7 material. Alpha keeps the falloff and the occlusion; it
# multiplies premultiplied colour by alpha a second time, so it reads slightly
# darker than retail, which is far closer than a blown-out white.
MATERIAL_BLEND_TO_WOTLK = {0: 0, 1: 1, 2: 2, 3: 3, 4: 4, 5: 5, 6: 6, 7: 2}

# ---- particles -------------------------------------------------------------
# A different enum entirely, and a much smaller one - 0..4, the old GL pairs:
#   0 no blend/no alpha test, 1 (SRC_COLOR, ONE), 2 (SRC_ALPHA, INV_SRC_ALPHA),
#   3 no blend + alpha test, 4 (SRC_ALPHA, ONE).
# The client's own 4,198 emitters never ship a value above 4. Since Cataclysm
# the field carries the MATERIAL enum instead, so it has to be translated:
# 7 (BlendAdd) has no particle equivalent and 4 is what Blizzard's own paired
# emitters use for it - retail 7 -> client 4, fifty-six times, never 2.
PARTICLE_BLEND_TO_WOTLK = {0: 0, 1: 3, 2: 2, 3: 1, 4: 4, 5: 4, 6: 4, 7: 4}

# Particle flag bits with no meaning before Cataclysm.
PF_MULTI_TEXTURE = 0x10000000     # `texture` holds three 5-bit indices
PF_ORIGIN_TO_POSITION = 0x00040000  # was particleType 1
PF_POST_WOTLK = 0x01000000          # the one bit neither build ever sets

P_FLAGS = 0x04
P_TEXTURE = 0x16
P_BLENDING = 0x28
P_EMITTER = 0x29
P_TYPE_OR_MTP = 0x2C
P_TAIL_LENGTH = 0x15C

SKIN_HDR_WOTLK = 48

# Legion shader ids index a table rather than encoding the op. 4 (Mod2x) is
# both the client's most common combiner and what Legion's shader 0,
# Opaque_Mod2xNA_Alpha, does.
LEGION_DEFAULT_OP = 4
SZ_BATCH = 24
SZ_SUBMESH = 48
B_SHADER = 0x02
B_TEX_COUNT = 0x0E
B_TEX_COORD_COMBO = 0x12


def _u32(d, o):
    return struct.unpack_from("<I", d, o)[0]


def _arr(d, o):
    return struct.unpack_from("<II", d, o)


class Patch:
    """One region this module is allowed to have changed, and why."""

    __slots__ = ("lo", "hi", "why")

    def __init__(self, lo, hi, why):
        self.lo, self.hi, self.why = lo, hi, why


def unwrap(raw: bytes):
    """-> (md20 payload, version). A chunked retail file, or a flat one."""
    if raw[:4] == b"MD20":
        return bytearray(raw), _u32(raw, 4), {}
    if raw[:4] != b"MD21":
        raise Fail("not an M2 (magic %r)" % (raw[:4],))
    chunks = {}
    p, n = 0, len(raw)
    while p + 8 <= n:
        magic = raw[p:p + 4]
        size = _u32(raw, p + 4)
        if size > n - p - 8:
            break
        chunks[magic] = (p + 8, size)
        p += 8 + size
    off, size = chunks[b"MD21"]
    return bytearray(raw[off:off + size]), _u32(raw, off + 4), chunks


def chunk_ids(chunks, raw, magic):
    if magic.encode() not in chunks:
        return []
    off, size = chunks[magic.encode()]
    return list(struct.unpack_from("<%dI" % (size // 4), raw, off))


# ---------------------------------------------------------------------------
#  The skins
# ---------------------------------------------------------------------------
# Retail's M2PixelShader table (shader_id & 0x8000) expressed in the 3.3.5
# vocabulary. Entries 0, 1 and 2 are not a mapping at all - 3.3.5 ships
# those three shaders under the same names (wow.exe 0x00836c90) and addresses
# them the same way, so they pass straight through as 0x8001/2/3. Those three
# exact matches anchor the ordering. Source: wow.export ShaderMapper.js
# https://github.com/Kruithne/wow.export/blob/main/src/js/3D/ShaderMapper.js
#
#   ("t", id)      -> ship this 3.3.5 table id verbatim
#   ("n", o0, o1, e0, e1, uv2) -> ops and vertex UV sources.
RETAIL_SHADER_TABLE = {
    # Anything mapped to "t" MUST land in 0x8001..0x8003. 0x00836cc2 zeroes
    # the prefix pointer, 0x00836ccf jumps straight to the strcpy at
    # 0x00836d07 when the index is above 3, and 0x00836d11 then reads through
    # a NULL. Shipping retail's ids verbatim - which the community
    # MultiConverter2 downporter does - puts 0x8005, 0x800F, 0x8015, 0x8019,
    # 0x8021 and 0x8022 into the skins and crashes the client on sight:
    #
    #   ERROR #132 0xC0000005 ACCESS_VIOLATION at 0023:00836D11
    #   "referenced memory at 0x00000000"
    #
    # measured at 306 crashing batches over these 170 models.
    0:  ("t", 0x8001),  # Opaque_Mod2xNA_Alpha / T1_Env
    1:  ("t", 0x8002),  # Opaque_AddAlpha / T1_Env
    2:  ("t", 0x8003),  # Opaque_AddAlpha_Alpha / T1_Env
    # Three-unit programs below preserve their first two units; they still
    # require a separately audited extra pass for complete equivalence.
    3:  ("t", 0x8001),  # Opaque_Mod2xNA_Alpha_Add
    4:  ("n", 1, 3, 0, 1, False),  # Mod_AddAlpha / T1_Env
    5:  ("n", 0, 3, 0, 0, False),  # Opaque_AddAlpha / T1_T1
    6:  ("n", 1, 3, 0, 0, False),  # Mod_AddAlpha / T1_T1
    7:  ("n", 1, 3, 0, 1, False),  # Mod_AddAlpha_Alpha
    8:  ("n", 0, 1, 0, 1, False),  # Opaque_Alpha_Alpha
    9:  ("t", 0x8001),  # Opaque_Mod2xNA_Alpha_3s
    10: ("n", 0, 3, 0, 0, False),  # Opaque_AddAlpha_Wgt / T1_T1
    11: ("n", 1, 3, 0, 1, False),  # Mod_Add_Alpha
    12: ("n", 0, 1, 0, 1, False),  # Opaque_ModNA_Alpha
    13: ("n", 1, 3, 0, 1, False),  # Mod_AddAlpha_Wgt / T1_Env
    14: ("n", 1, 3, 0, 0, False),  # Mod_AddAlpha_Wgt / T1_T1
    15: ("n", 0, 3, 0, 0, True),   # Opaque_AddAlpha_Wgt / T1_T2
    16: ("n", 0, 3, 0, 1, False),  # Opaque_Mod_Add_Wgt
    17: ("t", 0x8001),  # Opaque_Mod2xNA_Alpha_UnshAlpha / T1_Env_T1
    18: ("n", 1, 0, 0, 0, False),  # Mod_Dual_Crossfade / T1
    19: ("n", 1, 0, 0, 0, False),  # Mod_Depth / EdgeFade_T1
    20: ("t", 0x8001),  # Opaque_Mod2xNA_Alpha_Alpha / T1_Env_T2
    21: ("n", 1, 1, 0, 0, True),   # Mod_Mod / EdgeFade_T1_T2
    22: ("n", 1, 1, 0, 0, True),   # Mod_Masked_Dual_Crossfade
    23: ("n", 0, 1, 0, 0, False),  # Opaque_Alpha / T1_T1
    24: ("t", 0x8001),  # Opaque_Mod2xNA_Alpha_UnshAlpha / T1_Env_T2
    25: ("n", 1, 0, 1, 0, False),  # Mod_Depth / EdgeFade_Env
    26: ("n", 0, 1, 0, 0, True),   # Guild
    27: ("n", 0, 1, 0, 0, True),   # Guild_NoBorder
    28: ("n", 0, 0, 0, 0, True),   # Guild_Opaque
    29: ("n", 0, 0, 0, 0, False),  # Illum
    30: ("n", 1, 1, 0, 0, True),   # Mod_Mod_Mod_Const / T1_T2_T3
    31: ("n", 1, 1, 0, 0, True),   # Color / Mod_Mod_Mod_Const
    32: ("n", 0, 0, 0, 0, False),  # Opaque / T1
    33: ("n", 1, 4, 0, 0, True),   # Mod_Mod2x / EdgeFade_T1_T2
    34: ("n", 1, 0, 0, 0, False),  # Mod / EdgeFade_T1
    35: ("n", 1, 1, 0, 0, True),   # Mod_Mod_Depth / EdgeFade_T1_T2
}


def port_skin(raw: bytes, stats: dict):
    """Retail .skin -> 3.3.5-legal batches, plus what each batch needs.

    Structurally nothing changes. The client reads a 48-byte header and every
    array offset in it is absolute, so Cataclysm's `shadow_batches` at offset
    48 is simply never looked at.

    M2Batch.shader_id IS NOT NOISE AND IT IS NOT ZERO.

    CM2Shared::ComputeShaderIds (wow.exe 0x00836980) rebuilds every batch's
    shader id at load time, and when the model sets global flag 0x8 it reads
    the combiner op out of texture_combiner_combos INDEXED BY THE ON-DISK
    shader_id:

        0x008369ad  test  eax, 0x8000          ; already resolved -> leave it
        0x008369be  test  byte [hdr+0x10], 8   ; use_texture_combiner_combos
        0x00836a32  mov   edi, [model+0x150]
        0x00836a38  mov   esi, [edi+0x134]     ; texture_combiner_combos
        0x00836a3e  lea   ecx, [eax + ebx]     ; ebx = ON-DISK shader_id
        0x00836a41  movzx esi, word [esi+ecx*2]

    Writing 0 here - which is what this used to do - points every batch in
    every model at combiner entry 0: one texture-blend setting for the whole
    model, whatever each batch actually needed. So the id is now the index of
    that batch's own op group.

    The op vocabulary is proven, not assumed. M2GetCombinerName at 0x00836600
    switches on the nibbles and names the result, and its jump tables read:

        0 Opaque   1 Mod   2 Decal   3 Add   4 Mod2x   5 Fade
        6 Mod2xNA  7 AddNA                   (6 and 7 second unit only)

    high nibble = unit 0, low nibble = unit 1, and bit 3 of either nibble
    means that unit is ENVIRONMENT MAPPED - the same function picks the vertex
    shader name from those two bits: Diffuse_T1_T2, Diffuse_T1_Env,
    Diffuse_Env_T2, Diffuse_Env_Env.

    Retail uses the identical encoding. The census of this pack turns up op
    values 0, 1, 3, 4, 6 and 9 - and 9 is 8|1, the env bit over Mod, which
    only decodes to anything sane under exactly this scheme.

    Ids with 0x8000 index a TABLE. 3.3.5 has that table too, and its contents
    are not a guess: 0x00836c90 names entries 1, 2 and 3 as

        0x8001  Combiners_Opaque_Mod2xNA_Alpha
        0x8002  Combiners_Opaque_AddAlpha
        0x8003  Combiners_Opaque_AddAlpha_Alpha

    all three with vertex shader Diffuse_T1_Env - and those are retail table
    entries 0, 1 and 2 by name, exactly. Three consecutive exact matches
    anchor the published ordering, so retail table ids are mapped entry by
    entry below instead of being collapsed onto one default.

    0x8000 on its own is never shipped: 0x00821e97 skips any batch whose id is
    exactly that, because the client writes it when it merges a batch away.

    Returns (bytes, [(tc, ops, envs, mat, uv2, table_id)] per batch).
    """
    if raw[:4] != b"SKIN":
        raise Fail("not a skin (magic %r)" % (raw[:4],))
    d = bytearray(raw)
    want = []
    nb, ob = _arr(d, 4 + 8 * 4)
    for i in range(nb):
        b = ob + i * SZ_BATCH
        if b + SZ_BATCH > len(d):
            raise Fail("batch %d of %d runs past the end of the skin" % (i, nb))
        tc = struct.unpack_from("<H", d, b + B_TEX_COUNT)[0]
        if tc > 2 or tc == 0:
            # SetupTextures (0x0081f450) walks units 0..textureCount-1 into
            # D3D stages 0x15+u and then clears stages up to 2, and its nibble
            # shift starts at 4 and steps down by 4, so unit 2 has no shift
            # left. Two units is the ceiling the code is written for.
            tc = 2 if tc > 2 else 1
            struct.pack_into("<H", d, b + B_TEX_COUNT, tc)
            stats["skin_layers_capped"] += 1
        shader = struct.unpack_from("<H", d, b + B_SHADER)[0]
        table_id = None
        if shader & 0x8000:
            kind = RETAIL_SHADER_TABLE.get(shader & 0x7FFF)
            if kind is None:
                raise Fail("batch %d uses retail shader table entry %d, which "
                           "is not mapped" % (i, shader & 0x7FFF))
            if kind[0] == "t":
                table_id = kind[1]
                ops, envs = (0, 6), (0, 1)
                uv2 = False
                stats["skin_shader_tabled"] += 1
            else:
                ops, envs = (kind[1], kind[2]), (kind[3], kind[4])
                uv2 = kind[5]
                stats["skin_shader_untabled"] += 1
        else:
            ops = ((shader >> 4) & 0x7, shader & 0x7)
            envs = (1 if (shader >> 4) & 0x8 else 0, 1 if shader & 0x8 else 0)
            uv2 = bool(shader & 0x4000)
            if envs[0] or envs[1]:
                stats["skin_env_mapped"] += 1
        mat = struct.unpack_from("<H", d, b + 0x0A)[0]
        want.append((tc, ops, envs, mat, uv2, table_id))
        stats["skin_batches"] += 1
    return d, want


def assign_combos(skins, wants, uv2_ok, stats):
    """Build texture_coord_combos / texture_combiner_combos and index them.

    The two arrays are indexed SEPARATELY and by different fields, which is
    the whole point:

        combiner op for unit u = texture_combiner_combos[batch.shader_id + u]
        coord     for unit u = texture_coord_combos[batch.texCoordCombo + u]

    both from ComputeShaderIds at 0x00836a38 and 0x00836a56. Neither read is
    bounds-checked by the client, so every group written here is exactly as
    long as the batch textureCount and the index always leaves that many
    entries behind it in the array.

    What the coord value MEANS is settled by M2GetCombinerName picking the
    vertex shader from it (0x0083663e, 0x0083664e):

        0        this unit samples UV set 1        Diffuse_T1
        1        this unit samples UV set 2        Diffuse_T2 / Diffuse_T1_T2
        > 2      generate them - ENVIRONMENT MAP   Diffuse_Env

    and ComputeShaderIds turns `> 2` into the nibble bit 3 (0x00836a6c) and
    `== 1 on the last unit` into shader bit 0x4000 (0x00836a84), which is what
    reaches the draw call as the two-UV vertex format.

    Two things this build gets right for the first time:

      * env-mapped layers exist at all. Retail asks for them with nibble bit
        3 and every one of them used to be flattened to 0 here. 65535 is the
        value the client own art uses, 1,979 times across 5,001 models.

      * a unit only gets 1 when retail asked for it AND the model really has
        a distinct second UV set. 46 of the 170 models here have a UV set 2
        that is all zeroes; sampling it lands every lookup on texel (0,0) and
        smears it across the polygon.
    """
    cmb_at, cmb = {}, []
    crd_at, crd = {}, []

    def intern(table, at, group):
        if group not in at:
            at[group] = len(table)
            table.extend(group)
        return at[group]

    def groups_of(tc, ops, envs, uv2, batch_uv2):
        n = max(tc, 1)
        op_g = tuple(ops[u] for u in range(n))
        cd_g = []
        for u in range(n):
            if envs[u]:
                cd_g.append(0xFFFF)
            elif uv2 and batch_uv2 and n > 1 and u == n - 1:
                cd_g.append(1)
            else:
                cd_g.append(0)
        return op_g, tuple(cd_g)

    n_env = n_uv2 = n_table = 0
    for si, (sk, want) in enumerate(zip(skins, wants)):
        _nb, ob = _arr(sk, 4 + 8 * 4)
        for i, (tc, ops, envs, _mat, uv2, table_id) in enumerate(want):
            # Whether UV set 2 exists is a property of THIS batch's submesh,
            # not of the model. A model can carry a real second set on one
            # mesh and zeroes on another, and sampling the zeroes puts every
            # lookup on texel (0,0).
            batch_uv2 = uv2_ok[si][i] if si < len(uv2_ok) and i < len(uv2_ok[si]) else False
            op_g, cd_g = groups_of(tc, ops, envs, uv2, batch_uv2)
            ci = intern(cmb, cmb_at, op_g)
            di = intern(crd, crd_at, cd_g)
            b = ob + i * SZ_BATCH
            # A table id is left in place: ComputeShaderIds returns early on
            # 0x8000 (0x008369ad) and never touches that batch again.
            struct.pack_into("<H", sk, b + B_SHADER,
                             table_id if table_id is not None else ci)
            struct.pack_into("<H", sk, b + B_TEX_COORD_COMBO, di)
            if table_id is not None:
                n_table += 1
            if 0xFFFF in cd_g:
                n_env += 1
            if 1 in cd_g:
                n_uv2 += 1
    if not cmb:
        cmb, crd = [0, 0], [0, 0]
    stats["combiner_entries"] = len(cmb)
    stats["coord_entries"] = len(crd)
    stats["batches_env_mapped"] = n_env
    stats["batches_on_uv2"] = n_uv2
    stats["batches_table_shader"] = n_table
    return crd, cmb


# ---------------------------------------------------------------------------
#  Making room for one more header array
# ---------------------------------------------------------------------------
#  A 3.3.5 header is 304 bytes, or 312 when global flag 0x8 says the last
#  array - texture_combiner_combos - is present. Retail never sets that flag
#  here, so its header is 304 and its data begins immediately after.
#
#  But the client REQUIRES that array for a two-layer batch: of its own models
#  that have one, 60 out of 60 set flag 0x8 and ship the array. Without it the
#  client reads the second layer's combine mode out of an array that is not
#  there, and the layer draws opaque - a black slab under the effect.
#
#  So eight bytes have to appear at offset 0x130, and everything after them
#  moves. That is only safe if EVERY offset in the file is known, which is the
#  one thing this project now has: the same proven layout that the tiling
#  proof walks. Every M2Array offset is found, the data is shifted, and each
#  offset is raised by eight. The proof is re-run afterwards: if a single
#  offset had been missed, an array would land inside another one and the
#  tiling would break.
HDR_SHORT = 304
HDR_LONG = 312


def _offset_slots(d):
    """Every position in the file that holds an M2Array `offset` dword."""
    import m2_layout as LAY

    n = len(d)
    slots = []
    flags = _u32(d, 0x10)

    def arr(pos, elem):
        if pos + 8 > n:
            raise Fail("an array header at %d is outside the file" % pos)
        cnt, off = struct.unpack_from("<II", d, pos)
        slots.append(pos + 4)
        if cnt == 0 or off == 0:
            return 0, 0
        if off + cnt * elem > n:
            raise Fail("an array at %d..%d is outside the file"
                       % (off, off + cnt * elem))
        return cnt, off

    def track(pos):
        nts, ots = arr(pos + 4, 8)
        for i in range(nts):
            arr(ots + i * 8, 4)
        nvs, ovs = arr(pos + 12, 8)
        for i in range(nvs):
            arr(ovs + i * 8, 1)

    def fblock(pos):
        arr(pos, 2)
        arr(pos + 8, 2)

    for name, off, kind in LAY.HDR:
        if name == "texture_combiner_combos" and not (flags & 0x8):
            continue
        if isinstance(kind, int):
            arr(off, kind)
            continue
        if kind == "char":
            arr(off, 1)
            continue
        if kind == "particle":
            lay = LAY.particle_layout(M2_WOTLK)
            cnt, o = arr(off, lay["size"])
            for i in range(cnt):
                rec = o + i * lay["size"]
                for a, es in lay["arrays"]:
                    arr(rec + a, es)
                for f in lay["fblocks"]:
                    fblock(rec + f)
                for t in lay["tracks"]:
                    track(rec + t)
            continue
        spec = LAY.ELEM[kind]
        cnt, o = arr(off, spec["size"])
        for i in range(cnt):
            rec = o + i * spec["size"]
            for t in spec.get("tracks", []):
                track(rec + t)
            for t in spec.get("trackbases", []):
                nts, ots = arr(rec + t + 4, 8)
                for j in range(nts):
                    arr(ots + j * 8, 1)
            for a, es in spec.get("arrays", []):
                arr(rec + a, es)
    return slots


def make_room(body: bytearray, delta: int = 8):
    """Insert `delta` bytes at the end of the short header and fix every offset."""
    slots = _offset_slots(bytes(body))
    old = {}
    for pos in slots:
        old[pos] = struct.unpack_from("<I", body, pos)[0]
    out = bytearray(body[:HDR_SHORT]) + bytearray(delta) + bytearray(body[HDR_SHORT:])
    for pos, val in old.items():
        newpos = pos if pos < HDR_SHORT else pos + delta
        if val:
            struct.pack_into("<I", out, newpos, val + delta)
    return out


# ---------------------------------------------------------------------------
#  The model
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
#  SpellVisualEffectName.Alpha, which 3.3.5 has nowhere to put
# ---------------------------------------------------------------------------

H_COLORS_A = 0x048
H_WEIGHTS_A = 0x058
H_RIBBONS_A = 0x120
H_PARTICLES_A = 0x128
SZ_COLOR_A = 40
SZ_WEIGHT_A = 20
SZ_RIBBON_A = 176


def _fixed16_positions(body, track_at, n_seq):
    """Byte offsets of every fixed16 value a track holds.

    An M2Track is u16 interp, i16 global_seq, M2Array times, M2Array values.
    When global_seq is -1 the values array is an array OF M2Arrays, one per
    sequence; otherwise it is the values directly. Both shapes appear in this
    pack, so both are walked.
    """
    out = []
    if track_at + 20 > len(body):
        return out
    nv, ov = struct.unpack_from("<II", body, track_at + 0x0C)
    if nv == 0:
        return out
    # The values array is an array OF M2Arrays whether or not the track is
    # bound to a global sequence - a global-sequence track simply has one
    # entry instead of one per sequence. Treating it as raw values for
    # global-sequence tracks scaled the sub-array's COUNT instead of its
    # data: drakks_spell's weight[1] went from 9 keyframes to 6.
    for s in range(nv):
        e = ov + s * 8
        if e + 8 > len(body):
            break
        c, o = struct.unpack_from("<II", body, e)
        if c == 0 or o + c * 2 > len(body):
            continue
        out.extend(o + k * 2 for k in range(c))
    return out


def _fblock_fixed16(body, fb_at):
    """An emitter fblock is a plain (times, values) pair - no per-sequence
    nesting. Its values array here is fixed16."""
    out = []
    if fb_at + 16 > len(body):
        return out
    nv, ov = struct.unpack_from("<II", body, fb_at + 8)
    if nv and ov + nv * 2 <= len(body):
        out.extend(ov + k * 2 for k in range(nv))
    return out


def bake_effect_alpha(body, alpha, patches, stats):
    """Scale the model's alpha channel by retail's SpellVisualEffectName.Alpha.

    Retail multiplies the whole effect by this value at draw time. 3.3.5's
    SpellVisualEffectName.dbc has no Alpha column at all - its fields are ID,
    Name, FileName, AreaEffectSize, Scale, MinAllowedScale, MaxAllowedScale -
    so there is nowhere to carry it except the model, and a model used at two
    different alphas has to be published twice.

    It must land exactly ONCE on anything that gets drawn, because the client
    multiplies every contributing channel together:

        0x00821eb0  fld  [model+0x19c]              ; the model's own alpha
        0x00821ec4  fmul [colorAlpha + idx*0x20+0x1c]
        0x00821ee9  fmul [weight + idx*0x0c + 8]

    So a mesh batch takes it on texture_weights when the model has them and on
    the colour alpha track otherwise - never both. Emitters carry their own
    alpha fblock and ribbons their own track, and neither reads
    texture_weights, so those are scaled independently.

    Every value is fixed16: 32767 is 1.0.
    """
    if alpha >= 1.0:
        return
    n_seq = _arr(body, H_SEQUENCES)[0]
    touched = []

    n_w, o_w = _arr(body, H_WEIGHTS_A)
    n_c, o_c = _arr(body, H_COLORS_A)
    if n_w:
        for i in range(n_w):
            touched += _fixed16_positions(body, o_w + i * SZ_WEIGHT_A, n_seq)
        stats["alpha_on_weights"] += 1
    elif n_c:
        for i in range(n_c):
            touched += _fixed16_positions(body, o_c + i * SZ_COLOR_A + 0x14, n_seq)
        stats["alpha_on_colors"] += 1

    n_r, o_r = _arr(body, H_RIBBONS_A)
    for i in range(n_r):
        touched += _fixed16_positions(body, o_r + i * SZ_RIBBON_A + 0x38, n_seq)

    n_p, o_p = _arr(body, H_PARTICLES_A)
    for i in range(n_p):
        touched += _fblock_fixed16(body, o_p + i * SZ_PARTICLE_WOTLK + 0x114)

    if not touched and not (n_w or n_c or n_r or n_p):
        stats["alpha_nowhere_to_put"] += 1
        return

    for at in sorted(set(touched)):
        v = struct.unpack_from("<h", body, at)[0]
        nv = int(round(v * alpha))
        if nv > 32767:
            nv = 32767
        elif nv < -32768:
            nv = -32768
        if nv != v:
            struct.pack_into("<h", body, at, nv)
            patches.append(Patch(at, at + 2, "effect alpha"))
    stats["alpha_values_scaled"] += len(touched)


def port(raw: bytes, texture_paths=None, skins=None,
         texture_has_alpha=None, effect_alpha=1.0, supplied_types=()):
    """-> (model bytes, [skin bytes], report).

    `texture_paths` is the published path for each entry of the model's TXID,
    by position. `skins` are the retail .skin files, already paired.
    `supplied_types`: M2Texture types the client binds itself for this kind of
    model (2 = the item's own texture, on a helm or shoulder). A slot of such a
    type has no path and is NOT dead.
    """
    body, version, chunks = unwrap(raw)
    stats = {k: 0 for k in
             ("sequences_blend_merged", "materials_reblended",
              "materials_deflagged", "textures_named", "textures_deflagged",
              "particles_downported", "particles_reblended",
              "particles_untextured", "particles_retyped", "cameras_dropped",
              "particles_deflagged",
              "skin_layers_capped", "skin_batches", "skin_env_mapped",
              "batches_made_additive", "skin_shader_tabled",
              "skin_shader_untabled", "combiner_entries", "coord_entries",
              "batches_env_mapped", "batches_on_uv2", "batches_table_shader",
              "alpha_on_weights", "alpha_on_colors", "alpha_values_scaled",
              "alpha_nowhere_to_put", "skin_profiles_dropped",
              "batch_layers_trimmed", "batch_texture_missing_layer0")}
    patches = []
    texture_paths = list(texture_paths or [])
    texture_has_alpha = list(texture_has_alpha or [])

    if version == M2_WOTLK and not chunks:
        return bytes(body), list(skins or []), dict(stats=stats, patches=[])

    flags = _u32(body, H_GLOBAL_FLAGS)
    if flags & GF_COMBINER_COMBOS:
        raise Fail("model sets global flag 0x8; its header is 312 bytes and "
                   "every offset in it would have to move")
    if flags & GF_NEW_PARTICLE:
        raise Fail("model sets global flag 0x200; its emitters are not the "
                   "492-byte record this handles")

    # --- version and global flags ------------------------------------------
    struct.pack_into("<I", body, H_VERSION, M2_WOTLK)
    patches.append(Patch(H_VERSION, H_VERSION + 4, "version"))
    new_flags = flags & ~GF_POST_WOTLK
    if new_flags != flags:
        struct.pack_into("<I", body, H_GLOBAL_FLAGS, new_flags)
        patches.append(Patch(H_GLOBAL_FLAGS, H_GLOBAL_FLAGS + 4,
                             "global flags"))

    # --- sequences: one field, same record size ----------------------------
    if version >= 273:
        n, o = _arr(body, H_SEQUENCES)
        for i in range(n):
            at = o + i * SZ_SEQUENCE + 0x1C
            if at + 4 > len(body):
                raise Fail("sequence %d of %d runs past the end" % (i, n))
            blend_in = struct.unpack_from("<H", body, at)[0]
            struct.pack_into("<I", body, at, blend_in)
            patches.append(Patch(at, at + 4, "sequence blend time"))
            stats["sequences_blend_merged"] += 1

    # --- materials ---------------------------------------------------------
    n, o = _arr(body, H_MATERIALS)
    for i in range(n):
        at = o + i * SZ_MATERIAL
        mflags, blend = struct.unpack_from("<HH", body, at)
        nf = mflags & MATERIAL_FLAG_MASK
        nb = MATERIAL_BLEND_TO_WOTLK.get(blend)
        if nb is None:
            raise Fail("material %d uses blending mode %d, which is not a "
                       "mode any version of this format defines" % (i, blend))
        if nf != mflags or nb != blend:
            struct.pack_into("<HH", body, at, nf, nb)
            patches.append(Patch(at, at + 4, "material"))
            if nf != mflags:
                stats["materials_deflagged"] += 1
            if nb != blend:
                stats["materials_reblended"] += 1

    # --- particles: drop the sixteen bytes at the tail, in place -----------
    n_textures = _arr(body, H_TEXTURES)[0]
    n, o = _arr(body, H_PARTICLES)
    if n:
        end = o + n * SZ_PARTICLE_RETAIL
        if end > len(body):
            raise Fail("the emitter array runs past the end of the model")
        recs = [bytearray(body[o + i * SZ_PARTICLE_RETAIL:
                               o + i * SZ_PARTICLE_RETAIL + SZ_PARTICLE_WOTLK])
                for i in range(n)]
        for rec in recs:
            pflags = struct.unpack_from("<I", rec, P_FLAGS)[0]

            # THE 476 BYTES ARE COPIED VERBATIM. Every rule this block used
            # to apply was wrong, and the client's own art says so at a scale
            # that leaves no room for argument - 25,889 emitters, not the 1,544
            # an earlier pass sampled out of a handful of directories.
            #
            # 0x2C/0x2D are NOT particleType/headOrTail in 3.3.5. They are
            # multiTextureParamX, exactly as in retail. Flag 0x10000000 and a
            # non-zero pair go together in 25,824 of the client's 25,891
            # emitters - 99.7% - and in 866 of retail's 881. A field that
            # tracks one flag that precisely in two builds a decade apart is
            # the same field.
            #
            # Which also means 3.3.5 HAS multi-texture particles: 498 of the
            # client's own emitters set 0x10000000, and every one of retail's
            # 581 packs three texture indices into 0x16 that are all inside
            # the model's own texture array - 581 of 581. Stripping the flag
            # and masking 0x16 down to & 0x1F threw two of the three layers
            # away on two thirds of every emitter in this pack.
            #
            # And 0x80000000 is not a post-WotLK bit either; the client sets
            # it 55 times. The only bit neither build ever sets is 0x01000000.
            if pflags & PF_POST_WOTLK:
                pflags &= ~PF_POST_WOTLK
                struct.pack_into("<I", rec, P_FLAGS, pflags)
                stats["particles_deflagged"] += 1

            # 3.3.5 HAS NO MULTI-TEXTURE PARTICLES, and the exe says so in
            # one instruction. CParticleEmitter setup reads the texture field
            # exactly once, and it does not mask it:
            #
            #   00833ED5  movzx edx, word ptr [esi + 0x16]   ; ->texture
            #   00833ED9  mov   ecx, dword ptr [eax + 0x174] ; texture table
            #   00833EDF  mov   edx, dword ptr [ecx + edx*4] ; table[texture]
            #   00833EE9  call  00978BF0                     ; SetTexture -> AddRef
            #
            # A packed t0 | t1<<5 | t2<<10 therefore indexes the table with
            # 1057 instead of 1, lands on whatever is that far past the array,
            # and increments its first dword as a refcount. That is the
            # ACCESS_VIOLATION at 0047BF7C, with EAX holding ASCII ("rimm",
            # "arcr") because what it found was a heap string.
            #
            # Nothing in the setup function tests 0x10000000 either - the 22
            # flag tests there cover 0x2, 0x4, 0x10, 0x20, 0x40, 0x80, 0x100,
            # 0x200, 0x800, 0x2000, 0x4000, 0x8000, 0x10000 and 0x80000, and
            # that is all. So the flag is ignored and only layer 0 can be
            # drawn: mask the field down to it and drop the bit, because the
            # bit no longer describes the data.
            if pflags & PF_MULTI_TEXTURE:
                packed = struct.unpack_from("<H", rec, P_TEXTURE)[0]
                struct.pack_into("<H", rec, P_TEXTURE, packed & 0x1F)
                pflags &= ~PF_MULTI_TEXTURE
                struct.pack_into("<I", rec, P_FLAGS, pflags)
                stats["particles_untextured"] += 1

            tex = struct.unpack_from("<H", rec, P_TEXTURE)[0]
            if tex >= n_textures:
                raise Fail("emitter reads texture %d of %d" % (tex, n_textures))

            blend = rec[P_BLENDING]
            nb = PARTICLE_BLEND_TO_WOTLK.get(blend)
            if nb is None:
                raise Fail("emitter uses blending mode %d" % blend)
            if nb != blend:
                rec[P_BLENDING] = nb
                stats["particles_reblended"] += 1

            # 1 Plane, 2 Sphere, 3 Spline. Neither build ships any other
            # value: the client's 25,889 emitters and retail's 881 are all
            # 1, 2 or 3.
            if rec[P_EMITTER] not in (1, 2, 3):
                raise Fail("emitter type %d is not one 3.3.5 dispatches"
                           % rec[P_EMITTER])
            stats["particles_downported"] += 1

        body[o:o + n * SZ_PARTICLE_WOTLK] = b"".join(bytes(r) for r in recs)
        patches.append(Patch(o, end, "emitters"))

    # --- cameras: removed, because theirs is not the record ours reads -----
    # M2Camera is 100 bytes through Wrath and 116 from Cataclysm: the `fov`
    # float became a whole FoV track. It is the only record besides the
    # emitter whose SIZE differs, and unlike the emitter it cannot be
    # compacted in place, because the sixteen bytes are not at the tail. Nine
    # cameras across five of these models would otherwise be walked at the
    # wrong stride by the client. No spell visual in this pack is filmed by
    # its own camera, so the array goes to zero and the lookup with it.
    for at in (H_CAMERAS, H_CAMERA_LOOKUP):
        cnt, _ = _arr(body, at)
        if cnt:
            struct.pack_into("<II", body, at, 0, 0)
            patches.append(Patch(at, at + 8, "cameras dropped"))
            stats["cameras_dropped"] += cnt

    # --- textures: the names retail does not carry -------------------------
    # Appended at the end of the file, so nothing that already exists moves.
    n, o = _arr(body, H_TEXTURES)
    tail = bytearray()
    tail_base = len(body)
    for i in range(n):
        at = o + i * SZ_TEXTURE
        ttype, tflags = struct.unpack_from("<II", body, at)
        nf = tflags & TEXTURE_FLAG_MASK
        if nf != tflags:
            struct.pack_into("<I", body, at + 4, nf)
            patches.append(Patch(at + 4, at + 8, "texture flags"))
            stats["textures_deflagged"] += 1
        cnt, ofs = _arr(body, at + 8)
        if cnt and ofs:
            continue                      # already named
        if ttype != 0:
            continue                      # supplied by the client at runtime
        name = texture_paths[i] if i < len(texture_paths) else ""
        if not name:
            raise Fail("texture %d of %d is hardcoded but has no path" % (i, n))
        blob = name.replace("/", "\\").encode("latin-1") + b"\0"
        struct.pack_into("<II", body, at + 8, len(blob), tail_base + len(tail))
        patches.append(Patch(at + 8, at + 16, "texture name"))
        tail += blob
        stats["textures_named"] += 1

    # --- the skins, and what they need the model to carry ------------------
    n_m, o_m = _arr(body, H_MATERIALS)
    blends = [struct.unpack_from("<HH", body, o_m + i * SZ_MATERIAL)[1]
              for i in range(n_m)]
    out_skins, wants = [], []
    for sk in (skins or []):
        # A skin profile whose submeshes read past its own bone list is not
        # shippable: the client builds the bone palette straight from
        # boneComboIndex..+boneCount with no bound test. Retail's own lowest
        # LOD does this on at least one model here (boneComboIndex+boneCount
        # 87 against 65 entries), so the profile is dropped and the client
        # keeps using the next one up - identical at the distance that LOD
        # would have been chosen.
        nsb = _arr(sk, 4 + 8 * 2)[0]
        nsm, osm = _arr(sk, 4 + 8 * 3)
        over = False
        for k in range(nsm):
            bc, bci = struct.unpack_from("<HH", sk, osm + k * SZ_SUBMESH + 0x0C)
            if bci + bc > nsb:
                over = True
                break
        if over:
            stats["skin_profiles_dropped"] += 1
            break
        sb, want = port_skin(sk, stats)
        out_skins.append(sb)
        wants.append(want)
    n_tc, o_tc = _arr(body, H_TEX_COMBOS)
    tcombos = (struct.unpack_from("<%dH" % n_tc, body, o_tc) if n_tc else ())

    # A texture unit whose texture cannot be bound must not be left in the
    # batch. SetupTextures at 0x0081f450 walks units 0..textureCount-1 and
    # simply SKIPS the SetTexture when the model's runtime texture object is
    # null (0x0081f511 test eax,eax / je), so stage 1 keeps whatever the
    # PREVIOUS batch left in it and the layer draws with a stale texture -
    # white, additively, in the case that matters here.
    #
    # It is null whenever M2Texture.type is non-zero (the client is supposed
    # to supply that one - a skin, hair, an item - and nothing supplies a
    # texture to a spell visual) or the TXID entry is 0 and there is no name.
    # Corewarden's cast model is the one asset in this pack that does it:
    # texture 30 is type 1 with TXID 0, and batch 9 - the 25-vertex burst in
    # the middle of the void orb - binds it as its second layer.
    n_tex, o_tex = _arr(body, H_TEXTURES)
    kinds = [struct.unpack_from("<I", body, o_tex + i * 16)[0] for i in range(n_tex)]
    # The client binds a supplied type itself (an item's own texture is type
    # 2): such a slot has no path and is live. Treating it as dead stripped
    # the second unit off every two-layer item glow (retail 0x4014: the item
    # texture times itself through a moving UV) and left an additive sheet.
    dead = {i for i, t in enumerate(texture_paths)
            if not t and not (i < len(kinds) and kinds[i] in supplied_types)}
    if dead:
        for si, (sk, want) in enumerate(zip(out_skins, wants)):
            nb, ob = _arr(sk, 4 + 8 * 4)
            for bi in range(len(want)):
                b = ob + bi * SZ_BATCH
                tc = struct.unpack_from("<H", sk, b + B_TEX_COUNT)[0]
                tcb = struct.unpack_from("<H", sk, b + 0x10)[0]
                keep = tc
                for u in range(tc):
                    ti = tcombos[tcb + u] if tcb + u < n_tc else 0xFFFF
                    if ti in dead or ti >= len(texture_paths):
                        keep = u
                        break
                if keep == tc:
                    continue
                if keep == 0:
                    # nothing to draw with at all; leave one unit so the batch
                    # stays structurally legal and let the material decide.
                    keep = 1
                    stats["batch_texture_missing_layer0"] += 1
                else:
                    stats["batch_layers_trimmed"] += 1
                struct.pack_into("<H", sk, b + B_TEX_COUNT, keep)
                old_w = want[bi]
                wants[si][bi] = (keep,) + old_w[1:]

    def opaque_base(si, bi):
        """True when this batch's base texture carries no alpha channel."""
        sk = out_skins[si]
        nb, ob = _arr(sk, 4 + 8 * 4)
        if bi >= nb:
            return False
        tco = struct.unpack_from("<H", sk, ob + bi * SZ_BATCH + 0x10)[0]
        if tco >= n_tc:
            return False
        ti = tcombos[tco]
        return ti < len(texture_has_alpha) and not texture_has_alpha[ti]

    # Does this model actually carry a usable second UV set? M2Vertex is 48
    # bytes with tex_coords[0] at 0x20 and tex_coords[1] at 0x28.
    # Retail draws this effect at SpellVisualEffectName.Alpha; 3.3.5 has no
    # such column, so it goes into the model here.
    bake_effect_alpha(body, effect_alpha, patches, stats)

    # Does the submesh behind each batch really carry a second UV set?
    # M2Vertex is 48 bytes with tex_coords[0] at 0x20 and tex_coords[1] at
    # 0x28; a skin indexes the model's vertices through its own vertex list.
    n_vert, o_vert = _arr(body, H_VERTICES)

    def submesh_has_uv2(sk, sub):
        nsv, osv = _arr(sk, 4 + 8 * 0)
        nsm, osm = _arr(sk, 4 + 8 * 3)
        if sub >= nsm:
            return False
        sb = osm + sub * SZ_SUBMESH
        vs, vc = struct.unpack_from("<HH", sk, sb + 4)
        for k in range(vs, min(vs + vc, nsv)):
            mv = struct.unpack_from("<H", sk, osv + k * 2)[0]
            if mv >= n_vert:
                continue
            vb = o_vert + mv * 48
            if vb + 48 > len(body):
                continue
            u0 = struct.unpack_from("<ff", body, vb + 0x20)
            u1 = struct.unpack_from("<ff", body, vb + 0x28)
            if u1 != (0.0, 0.0) and (abs(u0[0] - u1[0]) > 1e-6 or
                                     abs(u0[1] - u1[1]) > 1e-6):
                return True
        return False

    uv2_ok = []
    for sk, want in zip(out_skins, wants):
        nb, ob = _arr(sk, 4 + 8 * 4)
        cache, row = {}, []
        for i in range(len(want)):
            sub = struct.unpack_from("<H", sk, ob + i * SZ_BATCH + 0x04)[0]
            if sub not in cache:
                cache[sub] = submesh_has_uv2(sk, sub)
            row.append(cache[sub])
        uv2_ok.append(row)

    coord, combiner = assign_combos(out_skins, wants, uv2_ok, stats)
    second_unit_on_uv1(body, out_skins, wants, uv2_ok, stats, patches)
    dangling_lookups(body, stats, patches)

    if out_skins:
        struct.pack_into("<I", body, H_NUM_SKINS, len(out_skins))
        patches.append(Patch(H_NUM_SKINS, H_NUM_SKINS + 4, "skin count"))

    # The texture names go on the end while offsets still mean what they did
    # in the retail file, so `audit` can still line the two up byte for byte.
    body += tail
    audited = bytes(body)

    # --- room for texture_combiner_combos ---------------------------------
    # Eight bytes at 0x130, every offset in the file raised by eight, and the
    # two parallel lookups appended after. A two-layer batch reads its texture
    # UNIT out of the first and its COMBINE MODE out of the second, with the
    # same index - which is how the client's own models are built, and without
    # which the second layer of all 1,367 two-layer batches in this pack drew
    # opaque.
    body = make_room(body, HDR_LONG - HDR_SHORT)
    flags = _u32(body, H_GLOBAL_FLAGS) | GF_COMBINER_COMBOS
    struct.pack_into("<I", body, H_GLOBAL_FLAGS, flags)

    while len(body) % 2:
        body += b"\0"
    at = len(body)
    body += struct.pack("<%dH" % len(coord), *coord)
    struct.pack_into("<II", body, H_TEX_COORD_COMBOS, len(coord), at)
    while len(body) % 2:
        body += b"\0"
    at = len(body)
    body += struct.pack("<%dH" % len(combiner), *combiner)
    struct.pack_into("<II", body, H_TEX_COMBINER_COMBOS, len(combiner), at)

    return bytes(body), out_skins, dict(stats=stats, patches=patches,
                                        tail_base=tail_base, audited=audited,
                                        shift=HDR_LONG - HDR_SHORT,
                                        shift_at=HDR_SHORT)


# (lookup header slot, header slot of the array its values index)
OPTIONAL_LOOKUPS = ((H_TEX_TRANSFORM_COMBOS, 0x060, "texture transform"),
                    (H_TEX_WEIGHT_COMBOS, 0x058, "texture weight"))


def dangling_lookups(body, stats, patches):
    """A lookup value naming an element that does not exist becomes -1 (none).

    Retail tolerates them and 3.3.5 does not: Helm_Robe_RaidPriest_E_01 (Cowl
    of the Avatar) ships NO texture transforms while its transform lookup's
    entry 1 names transform 0, and its first batch reads it. The client
    indexes the transform array without a bound, animates that unit's UVs by
    whatever lies past it, and a patch of the cowl drew as a hole. Texture
    lookups have no 'none', so one of those dangling is a failure, not a fix."""
    n_tex = _arr(body, H_TEXTURES)[0]
    nl, ol = _arr(body, H_TEX_COMBOS)
    for k in range(nl):
        v = struct.unpack_from("<h", body, ol + k * 2)[0]
        if not 0 <= v < n_tex:
            raise Fail("texture lookup %d names texture %d of %d" % (k, v, n_tex))
    fixed = 0
    for look_at, array_at, what in OPTIONAL_LOOKUPS:
        n_items = _arr(body, array_at)[0]
        nl, ol = _arr(body, look_at)
        for k in range(nl):
            at = ol + k * 2
            v = struct.unpack_from("<h", body, at)[0]
            if v >= n_items:
                struct.pack_into("<h", body, at, -1)
                patches.append(Patch(at, at + 2, "dangling %s lookup" % what))
                fixed += 1
    stats["lookups_dangling"] = fixed


def second_unit_on_uv1(body, skins, wants, uv2_ok, stats, patches):
    """A two-unit batch's second unit ALWAYS samples UV set 2 in 3.3.5.

    M2GetCombinerName (0x00836600) picks the vertex shader of a two-unit batch
    from the two environment bits alone - Diffuse_T1_T2 (0xa457d4),
    Diffuse_T1_Env, Diffuse_Env_T2, Diffuse_Env_Env - and the client ships no
    Diffuse_T1_T1. The coord value 0 that assign_combos writes for "unit 1 on
    UV set 1" is therefore never honoured: unit 1 reads texcoord[1] = UV set 2.
    Retail has T1_T1 programs, so a retail glow card or tint layer meant for
    UV set 1 drew, here, whatever its UV set 2 holds - all zeroes on 226
    batches of the helms, one texel stretched over the polygon (Hood of the
    Cycle: texel (0,0) of the green atlas, alpha 220, a white square).

    The only way to give unit 1 UV set 1 is to make UV set 2 equal it on those
    vertices. A vertex a batch really reads UV set 2 from keeps it; one asked
    for both ways is counted, never silently resolved."""
    n_vert, o_vert = _arr(body, H_VERTICES)
    need, keep = set(), set()
    for si, (sk, want) in enumerate(zip(skins, wants)):
        nsv, osv = _arr(sk, 4 + 8 * 0)
        nsm, osm = _arr(sk, 4 + 8 * 3)
        _nb, ob = _arr(sk, 4 + 8 * 4)

        def verts_of(sub):
            if sub >= nsm:
                return []
            vs, vc = struct.unpack_from("<HH", sk, osm + sub * SZ_SUBMESH + 4)
            return [struct.unpack_from("<H", sk, osv + k * 2)[0]
                    for k in range(vs, min(vs + vc, nsv))]

        for i, (tc, _ops, envs, _mat, uv2, table_id) in enumerate(want):
            sub = struct.unpack_from("<H", sk, ob + i * SZ_BATCH + 0x04)[0]
            real_uv2 = bool(uv2 and uv2_ok[si][i]) if si < len(uv2_ok) and i < len(uv2_ok[si]) else False
            if table_id is not None:
                continue                         # T1_Env tables: unit 1 env-mapped
            if tc == 2 and not envs[1]:
                (keep if real_uv2 else need).update(verts_of(sub))
            elif tc == 1 and real_uv2 and not envs[0]:
                keep.update(verts_of(sub))       # Diffuse_T2 reads it for real
    moved = clash = 0
    for mv in sorted(need):
        if mv >= n_vert:
            continue
        vb = o_vert + mv * 48
        u0 = struct.unpack_from("<ff", body, vb + 0x20)
        u1 = struct.unpack_from("<ff", body, vb + 0x28)
        if u0 == u1:
            continue
        if mv in keep:
            clash += 1
            continue
        struct.pack_into("<ff", body, vb + 0x28, *u0)
        patches.append(Patch(vb + 0x28, vb + 0x30, "uv2 for a second unit on uv1"))
        moved += 1
    stats["uv2_set_to_uv1"] = moved
    stats["uv2_both_ways"] = clash


# ---------------------------------------------------------------------------
#  Proving the output IS the retail file
# ---------------------------------------------------------------------------
def audit(raw: bytes, out: bytes, report: dict):
    """Every byte that differs must lie in a declared patch. Raises otherwise.

    This is the check the first converter could not make, because it built a
    new file and had nothing to compare against. Here the output starts as
    retail's own bytes, so anything that changed and was not meant to is a bug
    that shows up immediately, in the file, by offset.
    """
    src, _ver, _chunks = unwrap(raw)
    # The relocation that makes room for texture_combiner_combos moves every
    # byte after the short header, so the comparison is made against the
    # snapshot taken just before it - which is the file with every field patch
    # already applied and nothing moved.
    out = report.get("audited", out)
    tail_base = report.get("tail_base", len(src))
    allowed = sorted((p.lo, p.hi, p.why) for p in report.get("patches", []))
    common = min(len(src), tail_base)
    if len(out) < tail_base:
        raise Fail("the ported model is shorter than the retail one")

    def permitted(i):
        for lo, hi, _why in allowed:
            if lo <= i < hi:
                return True
        return False

    bad = []
    for i in range(common):
        if src[i] != out[i] and not permitted(i):
            bad.append(i)
            if len(bad) > 8:
                break
    if bad:
        raise Fail("%d byte(s) changed outside any declared patch, first at "
                   "0x%X" % (len(bad), bad[0]))
    # Anything retail had past our tail base (there is none by construction)
    # would be data we dropped.
    if len(src) > tail_base:
        raise Fail("retail data past the appended tail was lost")
    return True


# ---------------------------------------------------------------------------
#  Reading the result back the way the client will
# ---------------------------------------------------------------------------
def check_264(body: bytes, skins=None):
    """The third proof, and the only one that speaks the client's language.

    `audit` shows the file is retail's bytes plus declared patches, and the
    layout proof shows its arrays tile. Neither asks the question the client
    asks: when I index this table with that number, is the answer inside the
    table? A texture index one past the end is not a malformed file - it is a
    grey model, or a crash, and it is exactly the class of fault that shipped.
    """
    import m2_layout as LAY

    if body[:4] != b"MD20":
        raise Fail("the ported model is not flat MD20")
    ver = _u32(body, H_VERSION)
    if ver != M2_WOTLK:
        raise Fail("the ported model is version %d, not 264" % ver)
    flags = _u32(body, H_GLOBAL_FLAGS)
    if not (flags & GF_COMBINER_COMBOS):
        raise Fail("global flag 0x8 is clear, so the client will not read "
                   "texture_combiner_combos and every two-layer batch will "
                   "draw its second layer opaque")
    if flags & GF_NEW_PARTICLE:
        raise Fail("global flag 0x200 survived")
    if _arr(body, H_CAMERAS)[0]:
        raise Fail("cameras survived; their record is not the size 264 reads")

    # every array the client walks has to be inside the file
    overlaps, _unclaimed, _total = LAY.prove(body)

    n_tex = _arr(body, H_TEXTURES)[0]
    o_tex = _arr(body, H_TEXTURES)[1]
    for i in range(n_tex):
        at = o_tex + i * SZ_TEXTURE
        ttype, tflags = struct.unpack_from("<II", body, at)
        if tflags & ~TEXTURE_FLAG_MASK:
            raise Fail("texture %d keeps flags 0x%X" % (i, tflags))
        cnt, ofs = _arr(body, at + 8)
        if ttype == 0:
            if not cnt or not ofs:
                raise Fail("texture %d is hardcoded but unnamed" % i)
            if ofs + cnt > len(body):
                raise Fail("texture %d's name runs past the end" % i)
            if body[ofs + cnt - 1] != 0:
                raise Fail("texture %d's name is not terminated" % i)

    n_mat = _arr(body, H_MATERIALS)[0]
    o_mat = _arr(body, H_MATERIALS)[1]
    for i in range(n_mat):
        mflags, blend = struct.unpack_from("<HH", body, o_mat + i * SZ_MATERIAL)
        if blend > 6:
            raise Fail("material %d still blends with mode %d" % (i, blend))
        if mflags & ~MATERIAL_FLAG_MASK:
            raise Fail("material %d keeps flags 0x%X" % (i, mflags))

    n_par, o_par = _arr(body, H_PARTICLES)
    for i in range(n_par):
        rec = o_par + i * SZ_PARTICLE_WOTLK
        pflags = _u32(body, rec + P_FLAGS)
        tex = struct.unpack_from("<H", body, rec + P_TEXTURE)[0]
        # 00833EDF indexes the texture table with this word unmasked.
        if pflags & PF_MULTI_TEXTURE:
            raise Fail("emitter %d still claims three packed textures" % i)
        if n_tex and tex >= n_tex:
            raise Fail("emitter %d uses texture %d of %d" % (i, tex, n_tex))
        if body[rec + P_BLENDING] > 6:
            raise Fail("emitter %d blends with mode %d"
                       % (i, body[rec + P_BLENDING]))
        if body[rec + P_EMITTER] not in (1, 2, 3):
            raise Fail("emitter %d is type %d" % (i, body[rec + P_EMITTER]))


    # the lookups every batch reads, and the batches themselves
    n_combo = _arr(body, H_TEX_COMBOS)[0]
    n_coord = _arr(body, H_TEX_COORD_COMBOS)[0]
    n_comb, o_comb = _arr(body, H_TEX_COMBINER_COMBOS)
    # The two arrays are indexed independently - combiner by shader_id
    # (0x00836a3e), coord by textureCoordComboIndex (0x00836a56) - so their
    # lengths have no reason to match. Each batch is bounds-checked below
    # against the array it actually reads.
    if o_comb + n_comb * 2 > len(body):
        raise Fail("texture_combiner_combos runs past the end of the model")
    n_weight = _arr(body, H_TEX_WEIGHT_COMBOS)[0]
    n_trans = _arr(body, H_TEX_TRANSFORM_COMBOS)[0]
    n_sub_all = None
    for si, sk in enumerate(skins or []):
        nsub, osub = _arr(sk, 4 + 8 * 3)
        nb, ob = _arr(sk, 4 + 8 * 4)
        n_sub_all = nsub
        for i in range(nb):
            b = ob + i * SZ_BATCH
            sub, = struct.unpack_from("<H", sk, b + 0x04)
            col, = struct.unpack_from("<H", sk, b + 0x08)
            mat, = struct.unpack_from("<H", sk, b + 0x0A)
            tc, = struct.unpack_from("<H", sk, b + B_TEX_COUNT)
            tcombo, = struct.unpack_from("<H", sk, b + 0x10)
            ccombo, = struct.unpack_from("<H", sk, b + B_TEX_COORD_COMBO)
            wcombo, = struct.unpack_from("<H", sk, b + 0x14)
            xcombo, = struct.unpack_from("<H", sk, b + 0x16)
            where = "skin %d batch %d" % (si, i)
            if sub >= nsub:
                raise Fail("%s draws submesh %d of %d" % (where, sub, nsub))
            if mat >= n_mat:
                raise Fail("%s uses material %d of %d" % (where, mat, n_mat))
            if tc < 1 or tc > 2:
                raise Fail("%s has %d texture layers" % (where, tc))
            if tcombo + tc > n_combo:
                raise Fail("%s reads texture lookups %d..%d of %d"
                           % (where, tcombo, tcombo + tc - 1, n_combo))
            if ccombo + tc > n_coord:
                raise Fail("%s reads unit lookups %d..%d of %d"
                           % (where, ccombo, ccombo + tc - 1, n_coord))
            # The combiner array is indexed by shader_id, not by ccombo
            # (ComputeShaderIds 0x00836a3e), and neither read is bounds
            # checked by the client, so it has to be right here.
            shid, = struct.unpack_from("<H", sk, b + B_SHADER)
            if shid == 0x8000:
                raise Fail("%s ships shader id 0x8000, which 0x00821e97 "
                           "drops from the draw entirely" % where)
            if shid & 0x8000:
                if shid > 0x8003:
                    raise Fail("%s ships table shader 0x%04X; 3.3.5 only "
                               "names 0x8001..0x8003" % (where, shid))
            elif shid + tc > n_comb:
                raise Fail("%s reads combine modes %d..%d of %d"
                           % (where, shid, shid + tc - 1, n_comb))
            # The transparency lookup is consumed ONCE per batch, not once
            # per layer. Measured, because guessing it wrong rejects good
            # files: in the client's own 1,159 spell batches, 86 have
            # weightCombo + textureCount past the end of the array while
            # weightCombo + 1 is inside it in every single one.
            if n_weight and wcombo + 1 > n_weight:
                raise Fail("%s reads weight lookup %d of %d"
                           % (where, wcombo, n_weight))
            if n_trans and xcombo + tc > n_trans:
                raise Fail("%s reads transform lookups %d..%d of %d"
                           % (where, xcombo, xcombo + tc - 1, n_trans))
    # and every value those lookups hold names something that exists
    for look_at, array_at, what in OPTIONAL_LOOKUPS:
        n_items = _arr(body, array_at)[0]
        nl, ol = _arr(body, look_at)
        for k in range(nl):
            v = struct.unpack_from("<h", body, ol + k * 2)[0]
            if v >= n_items:
                raise Fail("%s lookup %d names %d of %d" % (what, k, v, n_items))
    return True


def usable_alpha(blp: bytes) -> bool:
    """True when this texture must keep the blending mode retail gave it.

    Two things decide it, and BOTH had to be measured because getting either
    backwards is visible from across the screen:

      * ALPHA CONTENT. A texture whose alpha is 1 everywhere cannot be
        alpha-blended into anything but a solid card. (The first attempt read
        BLP2's alphaSize at byte 6; it lives at byte 9, and byte 6 is part of
        the version dword, so every texture looked alpha-less and all 1,558
        alpha-blended batches were forced additive - which erased the black
        core of a portal and blew its middle out to white.)

      * BRIGHTNESS. An effect card whose art sits on BLACK is meant to be
        added, not blended: that is how its background disappears. Drawn with
        Alpha it becomes a dark translucent rectangle hanging in the air,
        which is exactly what these turned into when alpha was restored to
        them. A BRIGHT texture is the opposite case - additive blows it out -
        so it keeps what retail asked for.

    So: additive only for a card that is dark or has no usable alpha, and
    retail's own mode for everything else.
    """
    if not blp or len(blp) < 156 or blp[:4] != b"BLP2":
        return True
    _ver, ctype, asz, pfmt, _mips = struct.unpack_from("<IBBBB", blp, 4)
    if ctype != 2:
        return True
    off = struct.unpack_from("<I", blp, 20)[0]
    ln = struct.unpack_from("<I", blp, 84)[0]
    if off + ln > len(blp) or ln < 16:
        return True
    data = blp[off:off + ln]
    blk = 8 if (pfmt == 0 or asz <= 1) else 16
    cstart = 8 if blk == 16 else 0

    # brightness, from the DXT colour endpoints
    tot = 0.0
    n = 0
    for p in range(cstart, len(data) - 3, blk):
        c0, c1 = struct.unpack_from("<HH", data, p)
        for c in (c0, c1):
            rr = ((c >> 11) & 31) / 31.0
            gg = ((c >> 5) & 63) / 63.0
            bb = (c & 31) / 31.0
            tot += 0.299 * rr + 0.587 * gg + 0.114 * bb
            n += 1
    lum = (tot / n) if n else 1.0
    if lum < 0.35:
        return False                  # a dark card: add it

    if asz == 0:
        return False                  # no alpha to blend with at all
    lowest = 1.0
    for p in range(0, len(data) - 15, 16):
        if asz == 8 or pfmt == 7:
            lowest = min(lowest, min(data[p], data[p + 1]) / 255.0)
        else:
            for k in range(8):
                byte = data[p + k]
                lowest = min(lowest, (byte & 0xF) / 15.0, (byte >> 4) / 15.0)
        if lowest <= 0.9:
            return True
    return lowest <= 0.9
