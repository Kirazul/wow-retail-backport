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

Retail simulates every frame; 3.3.5 never does. A client mod runs retail's
own engine (rules.md section 6; reference `scripts/ref/physics_domino.cpp`).
The contract:
1. DATA, built offline per shipped file: the .phys fields copied RAW (the
   engine makes every conversion the client makes: degrees, clamps,
   defaults, mass): bodies (Domino type, bone, origin + the file's pivot
   shift, gravity / inertia scale, damping, carry, group), shapes (body,
   type, friction, restitution, density, capsule ends + radius, hull index),
   joints (type, bodies, motor mode, anchors, the file's frame rows, the six
   limit / motor floats), PLYT hulls whole (vertices, planes, face edges,
   half-edges, centroid, volume, area). Keyed by FNV-1a of the lowercased
   path with `/` -> `\` and `.mdx` -> `.m2`, plus the path (exact compare). A
   model with no row costs one cached lookup on its shared asset
   (`CM2Shared + 0x3C` holds its path).
2. HOOK: right after CM2Model::BuildBoneMatrices (0x0082F0F0; matrices at the
   pointer `CM2Model + 0x98`, row-vector, translation in 12..14; bone records
   at `CM2Shared + 0x150` -> +0x2C count, +0x30 bones, stride 0x58, parent
   int16 +0x08, pivot +0x4C), every read and write behind an exception guard.
3. SPACES: bone world = bone x S, S = `scene(CM2Model + 0x28) + 0xC4` (a
   MIRROR, det -1: never decompose it); the instance placement = the model's
   own matrix `CM2Model + 0xF4` x S (position, rotation, MODEL SCALE); write
   each dynamic body's model-space bone back as bone x `CM2Model + 0xF4`;
   carry every descendant bone by its parent's delta.
4. STATE per model instance (re-made when its shared asset changes,
   forgotten when unseen 2 s); the frame's time from the scene's clock
   (`scene + 0x0C`, ms; a second build in one frame takes 0 s; a scene whose
   clock stands still falls back to a high-resolution clock after 0.1 s);
   per contact the 12-byte GJK cache and the 8-byte SAT feature cache.
5. COST: every item steps every frame, as retail does (allowSleep is off for
   items in the client); watch it with a perf counter on the hook.
6. PROOF before shipping: rules.md section 6d (the client's own routines in
   unicorn vs the port; the whole simulation on every shipped file under the
   live glue), then the user in game.
Without a mod (or without the data row) the item shows retail's authored bind
pose: plumes and tentacles stand as modelled.

## 6. Proven in practice

Sections 1-4 have been implemented in a private 3.3.5a client extension
(bone-hash binding of retail pieces, appended tables, retail-race bodies) and
checked in game; section 5 (live physics) is implemented there and gated
offline (reference parity + P1), in-game confirmation pending. Every number in
this file comes from that work. Nothing in
this kit depends on any particular mod.
