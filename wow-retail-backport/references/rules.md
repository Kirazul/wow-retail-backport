# Every conversion rule, with its evidence

Each rule says WHAT the kit does, WHY (evidence: a retail table field, a wow.exe
3.3.5a 12340 address, wowdev.wiki, or a measurement with numbers), WHERE it is
coded (`scripts/lib/...`) and WHAT pins it (a test or a gate). A rule enters
this file only with all four. If something you believe contradicts a rule
here, the rule wins until you have MEASURED otherwise - then change both the
code and this file, with the new evidence.

Status words: **DONE** (implemented, gated), **HARD LIMIT** (3.3.5 has no
equivalent; dropped or approximated on purpose, reported), **TIER 2** (needs a
client mod, references/client-extension.md), **UNKNOWN** (not measured: never
assume; the "measure" column says how).

## 1. Data: which files an item is made of

| Rule | Evidence | Code | Pinned by |
|---|---|---|---|
| Everything comes from ONE pinned build: tables (`wago.tools/db2/<T>/csv?build=B`) and files (`wago.tools/api/casc/<fdid>?download&version=B`) | without a version wago serves the newest copy in ANY product; a model + .anim from two builds read other keyframes (bone 3 of anim 133: 18 vs 21 keys) | wago.py | init pins the build |
| Item -> look: TransmogSet -> TransmogSetItem -> ItemModifiedAppearance -> ItemAppearance (DisplayType = slot) -> ItemDisplayInfo | wowdev DB2 pages; every link a row | resolve.py | select output |
| Display -> files: ModelResourcesID -> ModelFileData -> ComponentModelFileData (RaceID, GenderIndex, ClassID, PositionIndex); ModelMaterialResourcesID -> TextureFileData -> ComponentTextureFileData; ItemDisplayInfoModelMatRes (TextureType, ModelIndex); ItemDisplayInfoMaterialRes (ComponentSection) | wowdev ItemDisplayInfo | resolve.attach_files | - |
| A model's own dependencies: SFID skins (LOD0 = 00.skin only), TXID textures, AFID .anim, SKID/SKPD skeleton, BFID bones, PFID physics, RPID/GPID particle models, followed recursively | wowdev M2 chunks | retail_m2.deps, resolve.fetch | newtech |
| A file served as all zeros is ENCRYPTED (unreleased at the build): never converted, never shipped, its display reported | measured on 12.1.5 content 2026-10-09 | wago.Files.read raises Encrypted | fetch report, build "Encrypted" |
| A 404/400 is "no such file at this build", not a failure | wago behaviour | wago._get | - |
| Retail race fallback: ChrRaces Male/FemaleModelFallbackRaceID/Sex is followed when a race has no cut of an item (Void Elf -> Blood Elf, Mag'har -> Orc ...) | retail's own rule; children share the parent's skeleton (SKPD) | build.cut_for | P1 |

## 2. Models (M2): retail -> version 264

The converter (`m2_port.port`) does NOT rebuild a model. It starts from retail's
bytes and changes only fields that provably differ, then PROVES it:
`m2_port.audit` (every changed byte is a declared patch) and `check_264` (read
back the way the client reads, every index in range). Measured over 141 retail
+ 399 WotLK models and 410 WotLK skins (m2_port.py docstring).

| Rule | Evidence | Status |
|---|---|---|
| MD21 payload lifted as flat MD20; header 304 bytes in both while global flag 0x8 is clear | m2_port point 1 | DONE |
| Global flags 0x20, 0x20000, 0x1000000, 0x20000000, 0x40000000 masked (post-WotLK); 0x8 set with texture_combiner_combos at 0x130 | m2_port; the client's own models never set the masked bits | DONE |
| Global flag 0x200 (newer emitter record): emitters dropped, mesh kept, reported `particles_dropped` | m2_port refuses to guess that layout | HARD LIMIT (count in build report) |
| Emitter 492 -> 476 bytes: drop the 16-byte tail; every nested array is at the same offset | searched 804 retail + 1,544 WotLK emitters | DONE |
| M2Sequence is 64 bytes in both; Warlords split blend_time into u16 in/out at 0x1C | m2_port point 4 (the first converter used 28 bytes and broke every animation) | DONE |
| Bone flags kept (every bit retail items set, WotLK sets too) | m2_port point 5 | DONE |
| Material flags masked to 0x13F (0x1 unlit 0x0081f201, 0x2 unfogged 0x0083395b, 0x4 two-sided 0x00833973, 0x8/0x10 depth 0x00833945/0x0083398e, 0x20 pass-1 blend 0x0081db7e, 0x100 no lights 0x0081f28f); 0x40/0x80 are Cataclysm shadow bits | wow.exe reads listed | DONE |
| Texture flags masked to 0x3 (wrap X/Y) | no WotLK texture sets 0x4 | DONE |
| Material blend: 0..6 kept; 7 (BlendAdd: ONE, INV_SRC_ALPHA) -> 2 (Alpha: same destination factor, keeps the falloff). Index 7 of the client's table (0x00a45570) is a string literal | the 7-entry table; no paired evidence exists for materials | DONE |
| Particle blend: its OWN enum (0..4); retail 1->3, 3->1, 5/6/7->4 | 56 emitters both games ship: retail 7 -> client 4, never 2 | DONE |
| Shader ids: Legion table ids (>= 0x8000) -> 0x8001..0x8003 or explicit op pairs + UV source; never > 0x8003 (NULL strcpy crash at 0x00836D11) nor exactly 0x8000 (dropped at 0x00821e97) | MultiConverter2 shipped raw ids: 306 crashing batches | DONE (check_264) |
| 3.3.5 has no Diffuse_T1_T1 (M2GetCombinerName 0x836600): a 2-unit non-env batch's unit 1 always reads UV2 -> UV2 = UV1 where retail meant UV1 | "white square" (texel 0,0) on a hood | DONE (second_unit_on_uv1) |
| ComputeShaderIds (0x836980): unit 0 forced Opaque at blend 0; without flag 0x8 unit 0 = Mod iff blend != 0 | wow.exe | DONE |
| More than 2 texture units: the first two kept (SetupTextures 0x0081f450 binds 2) | wow.exe | HARD LIMIT |
| Dangling lookups (value >= array) -> -1 | a hole in a cowl | DONE (check_264 rejects) |
| Unchecked client reads (texture_combiner_combos, texture_coord_combos, materials, bone matrices at centerBone, texture_weight_combos, sequence_lookups, variationNext chain) must be in range | verify_bounds.py lists each instruction | DONE (gate M2) |
| Vertex <= 65,535 per model, palette <= 75 bones per submesh, bone index < 256 | client limits (0x00837CDE reads the low byte) | DONE (check_264) |
| LOD0 skin only (`00.skin`) | the client reads 00.skin | DONE |
| Bounds grown to contain the mesh (never shrunk); filtered copies fitted to their own mesh | the client culls an attached model by its CURRENT sequence's box: items vanished on camera dips | DONE (gate M6) |
| Every vertex skins with its own bone: palette = boneLookup[submesh +0x0E boneComboIndex + vertex prop]; +0x10 is boneInfluences | a parser once swapped them: hoods skinned to a flame's billboard turned with the camera (8,820 helms) | DONE (gate M3) |
| A one-bone model's bone must not be a billboard (flags 0x8/0x10/0x20/0x40; the client turns it to the camera at 0x0082FFED) | Blind Oath crest | DONE (gate M4) |
| Edge fade (EDGF, EdgeFade_* shaders): mapped to the op pair without the fade | no 3.3.5 shader | HARD LIMIT |
| Every rewritten model (cut, filtered, shifted) is written with only referenced data and proven equal array by array (m2edit.repack) | 27.5 GB -> 2.0 GB on 50,688 pieces | DONE |

## 3. Animation

| Rule | Evidence | Status |
|---|---|---|
| External sequences: `<model><animId:04>-<sub:02>.anim` beside the model, AFM2 payload without retail's chunk wrapper | 3.3.5 naming (m2model.anim_name) | DONE |
| Only sequences whose keys really live in an .anim get one (items carry the character's whole sequence list with empty slots), only ids <= 505 (3.3.5 AnimationData) | measured: 399 empty slots on items | DONE (closure C2) |
| Sequence 0 (and Stand) must live in the model: the client reads it without an .anim | 7 crashes (0x00828565) | DONE (gate M5) |
| A colour/alpha track keyed in SOME sequences only, to one value, becomes a constant in every in-model sequence (3.3.5 evaluates an unkeyed sequence as 0 = black/invisible) | "black chest" on Goblin bodies | DONE (convert.constant_colours) |
| Global sequences kept (always looping) | format | DONE |
| UV animation (texture transforms) kept as retail bytes | format | DONE; parity UNKNOWN (measure: sheet + game) |
| Nothing flies, spins off or collapses: Stand + every global loop sampled 16 times; fail on a non-finite matrix, a vertex travelling > 3 m, or a collapse below 10 % | parts flying off reported in game | DONE (gate M8) |
| HelmetAnimScaling (retail scales helm animation per race: Goblin/Vulpera 0.8, Pandaren 0/0.4/0.5) | retail table | NOT CARRIED (affects how much animated helm parts move, not placement) |

## 4. Textures

| Rule | Evidence | Status |
|---|---|---|
| BLP2, sides <= 1024 (drop top mips; never resample unless re-encoding), never encoding 3, every mip inside the file | > 1024: the GPU gets one mip smaller and full rows are copied (heap corruption); encoding 3 overruns a memcpy when the quality setting drops a mip | DONE (gate T1) |
| The BLP2 header is ALWAYS 1172 bytes (148 + a 1024-byte palette block, DXT or not); mip data starts at 1172 or later | 0 of 6,113 retail BLP2 files put data earlier; a rewrite that dropped the block made small textures end inside the header the client reads | DONE (convert.tex335, gate T1) |
| Body-region (compositor) textures are PALETTISED (encoding 1) with 8-bit alpha when needed | the 3.3.5 compositor does not take DXT: a DXT region draws missing-texture GREEN | DONE (gate T2) |
| Palettised BLP alpha is decoded by the kit, never by Pillow (Pillow reads palette alpha 0) | retail Pandaren underwear went invisible | DONE (blp.decode, selftest) |
| Type 0 (file) textures: one copy per FileDataID at `Item\ObjectComponents\<ns>\<fdid>.blp` | - | DONE |
| Type 2 (the item's own texture) stays LIVE: the DBC row's ModelTexture supplies it, copied into the slot's folder (`Head`, `Shoulder`, `Weapon`, `Shield`, `Cape`) | the client builds `Item\ObjectComponents\<folder>\<name>.blp`; stripping it made crowns draw flat glow sheets | DONE |
| Types 3/4/24 (retail-only item slots, ItemDisplayInfoModelMatRes.TextureType) baked into the model per display; a colourway with other glow gets its own file (tag `__<sha8>`) | 3.3.5 binds only type 2 on an item | DONE |
| A combined set model's missing slots are completed from the displays wearing the same model resource in the same colourway (same type-2 texture), over ALL retail displays | each slot display hands only some slots; an unbound slot drew a white sheet (860 pieces) | DONE (build.Retail.supplies) |
| A batch still sampling an unbound slot is DROPPED (retail draws what we cannot bind; white is worse than nothing) | white sheets | DONE (gate M7) |
| Region names: section 0..7 -> ArmUpper, ArmLower, Hand, TorsoUpper, TorsoLower, LegUpper, LegLower, Foot (`Item\TextureComponents\<Region>Texture\<ns>\<name>_<U|M|F>.blp`) | ItemDisplayInfoMaterialRes.ComponentSection; the foot region IS composited (cmp edi,7 at 0x4ED7B1 counts layers) | DONE |
| A section shipped for one gender only also gets `_U` (the client asks `_<gender>` then `_U`) | client lookup order | DONE |
| Race-specific section textures (ComponentTextureFileData RaceID != 0) and section 8 (accessory) | 3.3.5 regions have no race | NOT CARRIED (reported per display) |
| Icons: retail icon (ItemAppearance.DefaultIconFileDataID, else Item.IconFileDataID) -> `Interface\Icons\<prefix>_<fdid>.blp`, <= 64 px | 3.3.5 icon frame | DONE |

## 5. Particles and ribbons

| Rule | Evidence | Status |
|---|---|---|
| EXP2 colorMult/alphaMult baked into each emitter's colour (0x104) and alpha (0x114, fixed16) blocks | 2 of 240 emitters != 1 (one draws at alpha 0 in retail) | DONE |
| ParticleColorID (display): emitters with particleColorIndex 11/12/13 (0x2A) get ParticleColor start/mid/end written into their own keys; selector cleared; one file per colourway | 335 displays; 3.3.5 ParticleColor rows are other rows | DONE |
| Multi-texture particles (flag 0x10000000): layer 0 only, flag cleared | the client indexes with the packed value: crash 0047BF7C | HARD LIMIT |
| Ribbons kept as retail bytes; alpha track fixed16 positions handled | format | DONE; parity UNKNOWN (sheet + game) |
| ItemVisual (enchant-like kits): 3.3.5 ItemVisuals rows are other rows | - | NOT CARRIED (reported) |

## 6. Physics (.phys) - read this whole section

**What retail does.** A model with physics names a `.phys` file (PFID) or
carries it inline (PFDC). Blizzard's runtime engine simulates it every frame:
BODIES on bones (type 0 = kinematic, follows its bone; type 1 = dynamic,
simulated, REPLACES its bone's transform), SHAPES (capsule, sphere, box,
convex polytope) with density/friction/restitution, JOINTS (spherical,
shoulder = cone + twist limits, weld = soft spring, revolute = hinge,
prismatic, distance), MOTORS that spring a joint back to its authored pose.
Chunk tags are stored byte-reversed (`SYHP` on disk = PHYS). Measured on 304
retail models (12.1.0.69933): all version 6; BDY4, SHP2, SHJ2/REV2/WLJ3,
CAPS, PLYT; joint angles are DEGREES (cones 12/30/45/60, twists +-2..20);
BDY 0x18 is a signed GRAVITY SCALE (< 0 floats: 92 % of those links sit above
their parent); SHJ2/REV2 motor = max torque, uint32 mode (1 = position), Hz,
damping ratio. Joint types used: shoulder 1,072, hinge 50, weld 16.

**What the parts rest on.** Retail CHARACTER bodies carry no physics at all
(no PFID/PFDC on the model, none in its .skel - measured on Human, Orc,
Blood Elf). The .phys file brings its own stand-ins for the wearer: shapes on
its KINEMATIC bodies (258 kinematic bodies with shapes: 255 capsules, 6
hulls). Hoods, plumes and chains rest on those.

**What 3.3.5 can do.** Nothing at runtime: the client has no physics engine.
The options, from most to least faithful:

| Option | Fidelity | Where | Status |
|---|---|---|---|
| Runtime physics in a client mod (Domino rules on the bone matrices each frame) | 1:1: swing while running, rest at idle | TIER 2, references/client-extension.md "Physics" | not built anywhere yet |
| Offline REST POSE: simulate at rest WITH contacts against the kinematic stand-ins, bake the settled pose into Stand | retail's idle rest pose (no swing) | physbake.settle (contacts DONE, tests pin them); baking it into shipped models is OFF until confirmed in game | study tool: `bp.py phys <fdid> --settle`, `--survey` |
| Retail's authored BIND pose (what ships today) | parts hang as modelled, never move | convert.py (default) | DONE |

The in-game failure (2026-10-09: settled helms beside the head) had one
PROVEN flaw behind it: the solver had NO contacts, so parts fell through the
stand-ins. Contacts are now in (`physbake.Contacts`: dynamic capsules and hull
vertices against kinematic capsules, static friction from the shapes'
friction, pairs overlapping in the bind pose left alone). The 3-test proof: a
part dropped on a stand-in rests ON it; with contacts off it falls through
(the bug); an authored overlap is not shot out.

What is NOT proven (pilot on 20 retail physics models, both settles, 2026-10-09):
all 20 settle (joint error <= 4.5 mm), but only 3 of 20 have parts that can
touch a stand-in (contacts move their rest by <= 3 cm). Most chains have
nothing to rest on and DROOP under gravity within their joint limits - e.g. a
Blood Elf plate helm's 3-link plume (no motor, 60 deg cones, half gravity)
settles 30-46 cm from its authored pose. Whether retail droops it that far is
UNKNOWN until compared with retail itself.

Measure before shipping any bake: settle one droop model (`phys <fdid>
--settle`), draw it, and compare with a retail screenshot or video of the same
item at idle; then a few more, then the user's in-game check. Only then make
baking the default here. Until then the authored bind pose ships.

Never "settle" without contacts again; never ship a bake that was not
compared with retail.

## 7. Geosets, slots, helms

| Rule | Evidence | Status |
|---|---|---|
| Body geosets from the row: boots 501 + GG0 (+901 kneepads), gloves 401 + GG0, sleeves 801 + GG0, robe 1301 + GG2, trousers 1101 + GG0, cape 1501 + GG0, belt 1801 + GG0; only GeosetGroup[0..2] exist in 3.3.5 | ApplyGeosets 0x004ED900 | DONE |
| A retail variant the client's bodies lack -> the variant of the closest SHAPE on that client (measured per client) | boots 506..510, kneepads 905, pants 1105, belts 1803/1804 absent on 3.3.5 bodies: the leg vanished | DONE (measure.geoset_map) |
| An item's OWN model geosets: a model used by displays of several slots (a combined set model) keeps only this slot's groups at group*100 + 1 + AttachmentGeosetGroup[col]; a model one slot uses (helm 27, shoulder 26, buckle 18) shows x01 + max(AGG, GeosetGroupOverride) | 1,125 of 1,125 combined cases; 158 of 158 override cases (the top-tier/Gladiator enhanced part) | DONE |
| Slot -> (group, AGG column): head 27/0 21/1; shoulder 26/0; chest 8/0 10/1 13/2 22/3 28/4; shirt 8/0 10/1; legs 11/0 9/1 13/2; hands 4/0 23/1; feet 5/0 20/1; waist 18/0; back 15/0; tabard 12/0 | WoW Model Viewer WoWItem.cpp + retail DB2 | DONE |
| Helm files per client race and sex: `Head\<ns>\<stem>_<ClientPrefix><M|F>.m2` (the client appends the suffix at 0x004E7800) | wow.exe; measured: every 3.3.5 helm path | DONE |
| Which cut: retail's own cut for the race (CMFD), else retail's fallback chain, else a race-agnostic model, else an INVISIBLE stand-in under the body's own name (a missing file is a checker box) | retail rule; checker boxes seen | DONE |
| A body-space helm (authored on the character's bones; median vertex height > 0.9) is made helm-point-local: one bone, every vertex minus retail's helm point | on point 11 it floats a body height up | DONE |
| Helm placement on a client body: + the measured helm shift of that body | references/bodies-and-races.md | DONE (gate P1) |
| Helm hides: retail HelmetGeosetData (vis id, race, HideGeosetGroup 0/1/2/3/7/16/17) -> 3.3.5 HelmetGeosetVisData's 7 race bitmasks (bit = 1 << client race id), new ids | 3.3.5 HelmetGeosetVisData 0x004EF1A0 | DONE |
| Shoulders: ModelName[0] = PositionIndex 0 (left), [1] = PositionIndex 1 (right) | measured on 2,193 3.3.5 shoulder rows (LShoulder_/RShoulder_) | DONE |
| Folders by item type: helm `Head`, shoulders `Shoulder`, weapons (InventoryType 13,15,17,21,22,23,25,26) `Weapon`, shields (14) `Shield`, quivers (18) `Quiver`, capes: texture in `Cape` | measured: every 3.3.5 Item.dbc row -> its model's folder | DONE |
| Belt buckles: a 3.3.5 belt draws no model | measured (no waist row names a buckle model) | TIER 2 |
| SheatheType retail -> 3.3.5: 0..4 same, 5 -> 0, 6 -> 6, 7 -> 7, 8 -> 7 | ~26,000 items both games share | DONE (selftest) |
| SheatheTransformMatrixID (per-display sheath offset) | no 3.3.5 equivalent | NOT CARRIED |
| Item class/subclass: same number when the names match (ItemSubClass of both games); Warglaives -> One-Handed Swords, Bear/Cat Claws -> Fist Weapons, Cosmetic/Relic -> Miscellaneous | measured on both tables | DONE (tables.SUBCLASS_FALLBACK) |

## 8. Tables and ids

| Rule | Evidence | Status |
|---|---|---|
| ItemDisplayInfo (25 fields), Item (8), HelmetGeosetVisData (8): the client's WINNING copy + appended rows; scan refuses another layout | 3.3.5 12340 layouts, checked on the client | DONE |
| Flags 0 (retail bits mean other things), SpellVisualID 0, ItemVisual 0, ParticleColorID 0 (baked) | - | DONE |
| GroupSoundIndex = the client's most common value for the same class/subclass/inventory type | measured from the client's own rows | DONE |
| New ids: displays above the client's maximum (rounded up to 1000), items from 900000 (configurable), helm vis above the client's; stable across runs (ids.json) | never collide with a client row | DONE |
| Server: the same three DBCs in its dbc folder + item_template SQL (AzerothCore / TrinityCore 3.3.5 column names) | the server validates item displays against ItemDisplayInfo.dbc | DONE |
