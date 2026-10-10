# Tier 2: what a client mod must do (and what is already known)

A stock 3.3.5a client draws an item from its ItemDisplayInfo row: textures on
the body, geosets, and models hung on attachment points (helm, shoulders,
weapons, shield, quiver). Retail draws much more. This file is the contract a
client extension (a DLL loaded by the game client) must meet to
show it, with every fact measured so far. `build --extension` exports the
data such a mod needs: `out/extension/extension.json` +
`out/extension/<patch>-ext.MPQ`.

Ground rules for any mod: never override the game (only add, under private
paths and ids; a player without the mod's data sees no change), zero work per
frame that is not needed (cache per model pair), and every hook guarded so a
fault leaves the client's own behaviour.

## 1. Retail 3D pieces ("collections": collars, skirts, boot shells, gauntlets, belts, 3D capes)

| Fact | Evidence |
|---|---|
| Retail cuts each piece PER RACE AND SEX (ComponentModelFileData) and SKINS it to the character's bones; it is not hung on one joint | retail DB2; rigid fitting always floated or tore (thousands of reports, 2026-10-04/05) |
| A retail cut carries the retail body's own bind pivots for every weighted bone (0.00000 m difference, 5 races) and weights only body bones | measured |
| Retail bone NAME HASHES (M2Bone +0x0C) identify joints across bodies; HD 3.3.5 bodies share them (Orc M: 211 of 220) | measured |
| The client hangs an attached model on ONE point; its bone matrices are model-to-view; CM2Model+0x98 is a POINTER to the matrix array; 0x00830DC0 animates a child's parent first | wow.exe 12340 |

The contract:
1. Load the piece as an attached model (any point).
2. After the client builds its matrices (BuildBoneMatrices 0x0082F0F0), replace
   each piece bone's matrix by the BODY's matrix of the bone with the same name
   hash (one 4x4 copy per bone, cached per (piece model, body model) pair,
   rebuilt when either changes). Bones the body lacks: follow the nearest mapped
   ancestor (retarget X = T(P_body) R S T(-P_retail), identity on retail and HD
   bodies within 1 mm).
3. Own bones of the piece (cloth, animated parts, glow cards) keep their
   animation, re-parented to the bound parent; a billboard keeps facing the camera.
4. Geosets of the piece: the slot's groups at group*100 + 1 +
   AttachmentGeosetGroup (rules.md section 7); a chest robe owns group 13 (the
   legs' 13xx is off under a robe chest).
5. Legacy (stock low-poly) bodies share the core skeleton by hash but not its
   proportions (shared joints 0-0.67 m apart): skinning retail cuts to them
   does not fit. What works: an OFFLINE SURFACE TRANSFER per body - each piece
   vertex = retail skin point + height along its normal -> the corresponding
   point on the legacy skin at the same height, weights = the legacy skin's own
   weights there (the piece moves exactly as the skin).
   Then clear it out of the skin in bind and in Stand/Walk/Run, AGAIN after
   skinning on the file's own 8-bit weights and tagged bones (what the client
   blends), at most 2 cm per point: clearing only on the float weights left
   skin through 9.7% of a pair of pants while running (quantisation sank the
   cleared points); clearing without a cap chased a leg swinging through the
   cloth 31 cm out (spikes).
7. PACK EVERY FILE THE MOD NAMES AT RUNTIME. A mod that builds per-body file
   names itself (`<stem>_<race><sex>[family].m2`) names files no table row
   names: the archive closure and its verify must enumerate them from the
   mod's own naming rule, or every such piece vanishes in game while the
   verify reports 0 missing (measured: 61,790 files left out once).
6. extension.json lists, per new display id, each piece file per client race
   and sex (`skinned_to_retail_bones` true = bind by name hash).

## 2. Belt buckles

Retail: a race-agnostic model on attachment 53 (ATT_BELT_BUCKLE). 3.3.5 belts
draw no model and bodies may lack point 53: the mod adds the point (or uses
the closest waist point) and attaches the buckle (geosets: group 18 at x01 +
max(AGG, override)).

## 3. Geoset groups 3.3.5 does not know

ItemDisplayInfo.GeosetGroup[3..5] (chest 22xx/28xx, hands 23xx, feet 20xx).
The client applies only [0..2] (ApplyGeosets 0x004ED900) and never hides a
geoset id > 2000. A mod mirrors ApplyGeosets with retail's columns (prove it: run wow.exe's
own function in an emulator against the mod's over thousands of outfits -
one implementation measured 0 differences over 6,000). Feet: boots GeosetGroup[1]: -1 = keep the default foot 2001,
0 -> 2002, k -> 2000 + k.

## 4. New retail races (bodies the client does not have)

Retail bodies converted 1:1 (skeleton from .skel, every sequence needed by
3.3.5 resident or as .anim, colour tracks made constant, textures by section,
customisation from ChrCustomization*), each with its own prefix; helms then
resolve as `<stem>_<Prefix><Sex>.m2` from retail's own per-race cuts, pieces
bind by name hash (section 1). Hard-won facts: sequence 0 and Stand must be
resident; a race without a Stop pose flickers in UI previews (add one);
compositor face regions draw the base skin only if the CharSections row has
flag 0x8; only geosets <= 2000 are hidden by the client (the mod owns 20xx and
>= 21); `<= 65,535` vertices, palette <= 75 bones, bone index < 256.

## 5. Physics (.phys): retail motion, live

Retail simulates every frame; 3.3.5 never does. The contract (built in a
private client extension and gated offline; rules.md section 6 has every
rule's evidence):
1. DATA, built offline per shipped file: the model's .phys (physread.py: tags
   byte-reversed; BDY4/SHP2/SHJ2/REV2/WLJ3 version 6; angles DEGREES; BDY 0x18
   = signed gravity scale; motors: max torque, mode 1 = position, Hz, damping
   ratio) digested into a fixed binary row: bodies (kinematic flag, bone,
   origin + the file's pivot shift, gravity scale, damping, inverse mass,
   centre of mass, inertia and its inverse), capsules (body-local), joints
   (anchors, orthonormal frames, limits, cone, weld Hz/ratio, motor), keyed
   by FNV-1a of the lowercased path with `/` -> `\` and `.mdx` -> `.m2`, plus
   the path itself (exact compare after the hash). A model with no row costs
   one cached lookup on its shared asset (`CM2Shared + 0x3C` holds its path).
2. HOOK: right after the client builds a model's bone matrices
   (CM2Model::BuildBoneMatrices 0x0082F0F0, once per model per frame; matrices
   at the pointer `CM2Model + 0x98`, row-vector, translation in 12..14). Read
   the bones and their parents (shared M2 header at `CM2Shared + 0x150`: +0x2C
   count, +0x30 bones, stride 0x58, parent int16 at +0x08), every read and
   write behind an exception guard.
3. WORLD SPACE: world = bone x `scene(CM2Model + 0x28) + 0xC4` (row-vector);
   simulate there (physlive.Model.step), write each dynamic body's bone back
   through that matrix's inverse, carry every descendant bone by its parent's
   delta.
4. STATE per model instance (keyed by the model, re-made when its shared
   asset changes), forgotten when unseen for 2 s; time from a high-resolution
   clock; the resets of rules.md section 6.
5. BUDGET: about 0.25 ms of physics a frame; past it a load level L makes each
   item step every 2^L frames on its own phase and ride rigidly with its root
   between (catching up the missed time, capped at 6 substeps). Measured cost
   about 6.5 us per item per frame.
6. PROOF before shipping: the mod's solver against `physlive.py` on the same
   rows and frames (rules.md section 6 "Proof"), the glue (matrix round trip,
   descendant carry in either bone order, rigid carry), gate P1 over every
   setup, then the user in game.
Without a mod (or without the data row) the item shows retail's authored bind
pose: plumes and tentacles stand as modelled.

## 6. Proven in practice

Sections 1-4 have been implemented in a private 3.3.5a client extension
(bone-hash binding of retail pieces, appended tables, retail-race bodies) and
checked in game; section 5 (live physics) is implemented there and gated
offline (reference parity + P1), in-game confirmation pending. Every number in
this file comes from that work. Nothing in
this kit depends on any particular mod.
