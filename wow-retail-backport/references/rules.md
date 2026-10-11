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

**What 3.3.5 can do.** The client has no physics engine, but a client mod can
run RETAIL'S OWN ENGINE: Blizzard's "Domino" (a 3D Box2D-2.x-family solver),
ported instruction by instruction from the Midnight 12.1.0.69933 client
(macOS x86_64, __TEXT decrypted by emulating its first mod_init in unicorn,
read with capstone; MoP 5.4.8 Wow-64.exe as a readable cross-check). The
reference implementation is `scripts/ref/physics_domino.{h,cpp}` (pure C++,
no client memory; every block names the retail function it was read from).
Nothing in it is tuned. Status: ported 2026-10-11; narrowphases proven
against retail's machine code (below); in-game confirmation pending.

| Option | Fidelity | Where | Status |
|---|---|---|---|
| RUNTIME physics in a client mod: retail's engine, its data, its constants | 1:1 (bit-exact where proven, see Proof) | Tier 2, client-extension.md section 5; reference `scripts/ref/physics_domino.cpp` | built, proven offline; in-game confirmation pending |
| Offline REST POSE baked into Stand | retail's idle rest pose, no swing | physbake.settle | study tool only (`phys <fdid> --settle`, `--survey`) |
| Retail's authored BIND pose | parts stand as modelled, never move | convert.py (default; what a stock client shows) | DONE |

Superseded (do not use): the 2026-10-10 XPBD "item rides its wearer /
bounded inertia / 1/240 s substeps / sleep" solver. It was ours, not
retail's; the engine below replaces it.

### 6a. The data (.phys v6; parser 0x1011AA600, upgrader 0x1011AAC90)

Chunks in all 304 retail physics files: PHYS (v6), PHYT (u32), BDY4, SHP2,
CAPS, JOIN, SHJ2; REV2 (45 files), WLJ3 (5), PLYT (21). Record sizes: BDY4
0x30, SHP2 0x20, CAPS 0x1C, JOIN 0x10, SHJ2 0x7C, REV2 0x78, WLJ3 0x74.
- BDY4: 0x00 u16 type (0 -> Domino KINEMATIC 1, 1 -> DYNAMIC 0, else static
  2); 0x02 bone; 0x04 pos[3]; 0x10 u16 first shape; 0x14 i32 shape count;
  0x18 GRAVITY scale (negative floats up); 0x1C INERTIA scale; 0x20 LINEAR
  damping; 0x24 ANGULAR damping; 0x28 CARRY (default 0.9, data mostly 0.01);
  0x2C u16 group (0x8000 head, 0x4000 instance root). Proven through the
  game's setters (shuffles 0x1E at 0x1011AD7C0 and 0x93 at 0x1005E78D0).
- SHP2: i16 type (1 capsule, 2 sphere, 3 PLYT hull, 0 box), i16 index, +8
  friction, +0xC restitution, +0x10 density. CAPS: a[3], b[3], r (body local).
- PLYT: u32 n, n x 0x50 headers {+0 nverts, +0x10 nfaces, +0x28 nhalfedges,
  +0x38 centroid[3], +0x44 volume, +0x48 area}, then per hull: vertices
  (12 B), planes (16 B: n, d with n.x = d), each face's first half-edge
  (u8), half-edges (4 B: int8 TWIN OFFSET, origin vertex, face, next).
- JOIN: u32 a, u32 b, pad, u16 type (1 SHJ2 -> Domino 7 shoulder, 3 REV2 -> 6
  revolute, 2 WLJ3 -> 9 weld, 0 SPHJ -> 8, 4 PRS2 -> 5, 5 DSTJ -> 2), u16
  index. Frames: 3x3 with the AXIS IMAGES AS ROWS + pos (A at 0x00/0x24, B at
  0x30/0x54); positions x model scale.
- SHJ2 0x60 twistLo, 0x64 twistHi, 0x68 cone (DEGREES), 0x6C maxTorque,
  0x70 int8 mode, 0x74 Hz, 0x78 zeta (defaults Hz 1, zeta 0.7, mode 0).
  REV2 0x60 lower, 0x64 upper (deg), 0x68 maxT, 0x6C int8 mode, 0x70 Hz,
  0x74 zeta. WLJ3 0x60 angHz, 0x64 linHz, 0x68 angZeta, 0x6C linZeta.

### 6b. The engine (Midnight Mac VAs; full detail in the reference's comments)

| Stage | Rule |
|---|---|
| Instance (PhysicsRagdoll, world list +0x78) | its OWN private world (0x1005C98F0), gravity (0, 0, -10). Bodies: pos = Rot(q, pos) * scale + t, q = the placement's rotation; gyroscopic ON, angular cap ON, twist friction ON, allowSleep OFF (items never sleep). Fixtures: friction 0.6, restitution 0, ROLLING 0.2, hull radius 0.01, contact 10 Hz / 0.7; every shape of one item collides with every other except on the same body, on jointed bodies (collideConnected 0) and between two non-dynamic bodies; mix: friction sqrt(fA fB), restitution clamp(eA + eB, 0, 1), rolling sqrt |
| Mass (0x1005E8270) | m = density x volume over shapes with finite density > 0 (non-dynamic: density 1); capsule (h pi + 4.18879 r) r^2, hull = file volume; centre = sum m c / M; M <= 1.18e-35 -> M = 0, im = 0 (NO fallback to 1: a shape-less dynamic body has no mass). Inertia about the COM (capsule 0x1005E1890, hull = Eberly polyhedral integrals 0x1005DCF30) x inertia scale, inverted when det > tiny |
| Frame (0x1011ABC30) | dt = min(frame dt, 1/30); inv = 1/dt; invc = min(inv, 45); linLo 0.25 invc, linHi 0.5 invc, angLo pi/8 invc, angHi pi/4 invc. Order: drive -> step each instance (only when ALL its bodies are awake; 8 velocity / 12 position iterations) -> bones |
| Drive (0x1011B3790 / 0x1011B4370) | placement split: axis lengths (guard 2.38e-7), all > 0.016 -> normalise, det < 0 -> negate the third (THE MODEL'S placement - see 6c), scale = max(|axis 1|, 0.016); scale change > 0.1% -> rescale bodies. First frame: every body to its bone's world pivot with the placement's rotation. Each kinematic body: target = bone world pivot + normalised axes; s = max(fracAngular, fracLinear, carry) where frac(x) = 0 under lo, 1 over hi, else carry + (1 - carry) (x - lo)/(hi - lo); s of the move is a TELEPORT (nlerp + COM shift), the rest velocity; the group head (0x8000) carries the following dynamic bodies by its delta (the root's delta when a 0x4000 root exists) |
| Island step (0x1005F2E40) | dtRatio = min(inv_dt0 dt, 2); dynamic: implicit gyroscopic Newton step in body space, v += h im (m gs g), damping v *= clamp(1 - h ld, 0, 1); prepare joints then contacts (warm start x dtRatio); 8 iterations joints then contacts; integrate: non-finite -> 0, |h v| <= 4, |h w| <= pi/4 (rsqrt + Newton), q renormalised; 12 position iterations, joints then contacts, NO early exit |
| Shoulder (0x1006021C0 / 0x100603590 / 0x1006039B0) | swing = atan2approx(\|a x b\|, a.b) of the frames' Z; twist = 2 atan2approx(rel.z, rel.w); twist on iff lo < hi, \|hi - lo\| < 4 deg locks; cone clamped [10, 170] deg; motor mode 1 = soft position per axis (2 pi Hz, zeta), mode 0 off, else velocity capped by maxTorque; position pass clamps (err -+ 2 deg) to 30 deg x 0.2; atan2approx: \|r\| < 1: r / (1 + 0.28 r^2), else +-pi/2 - r / (r^2 + 0.28) |
| Revolute (0x1005FF410 / ...3C0 / ...840) | hinge = Rot(fA, Z); lower clamped [-2 pi, 2 pi], centre/half of the range; 2-row lock; limit states as Box2D; soft position motor |
| Weld (0x1006064E0 / ...7320 / ...7690) | angular soft when angHz > 0 (c = 4 pi Hz zeta m, k = (2 pi Hz)^2 m), impulse capped pi/4 inv trace(I); linear soft when linHz > 0; position pass only for the rigid parts |
| Contacts (prepare 0x1005EC2E0, velocity 0x1005ED970, position 0x1005EE250) | soft (10 Hz, 0.7) when both dynamic with mass ratio > 10 or one kinematic; restitution under -2 yd/s; 2D friction at the manifold centre + twist friction (mu x 0.5 x RMS radius) + ROLLING resistance; position: rigid points only, beta 0.2, slop 0.005; broadphase fat AABB +- 0.1, refit past 4 margins |
| Capsule vs capsule (0x1005CD880) | exact segment closest points incl. parallel (cos^2 >= 0.998782) and degenerate; point between the surfaces |
| Hull vs capsule (0x1005DC920 -> 0x1005DC020) | GJK (b2Distance in 3D, warm-started from the contact's 12-byte cache {metric, idxA[4], idxB[4]}, kept within 0.25..4 x metric, 20 iterations). No contact past r + 0.01. A clear gap (>= 0.005, simplex not full): the face within 5 deg (0.99619472) of the GJK normal is clipped (0x1005DBAB0), else one point keyed from the simplex. Deeper: SAT over faces, then edges whose Gauss-map arcs the segment crosses (an edge wins only past 0.9 x best + 0.0025); edge contact = segment closest points (0x1005D1780) |
| Hull vs hull (0x1005E11D0 -> 0x1005E0500) | Gregorius' SAT with a per-contact 8-byte FEATURE CACHE {sep, idx, idx, type 1 A face / 2 B face / 3 edge pair}: last frame's feature is re-tested and reused while its separation moved < 0.005. Full SAT: faces of A, faces of B (must beat 0.98 x best + 0.0025), edge pairs (Gauss-map test from edge vectors, normal EA x EB oriented by the larger centroid offset); any positive separation is cached and ends it. Face contact: the incident face (the face around the support vertex most along -n) clipped to the reference face's side planes PUSHED OUT BY 0.02, points within 0.02 PROJECTED onto the reference face; more than 4 reduced (0x1005DB4A0: along a tangent, furthest, largest triangle, furthest outside; 0.99 hysteresis, 0.02^2 floor). The edge pair wins if edgeSep > 0.9 faceSep + 0.0025 with < 2 face points, or edgeSep > 0.9 min(point seps) + 0.0025. Margin 0.02 = both 0.01 radii |
| Bones (0x1011B4BF0) | dynamic bodies only: bone = [conj(q_inst) q_body \| conj(q_inst)(origin - pos_inst) / scale] x T(-pivot), MODEL space |
| Numerics | rsqrtss / rcpss + ONE Newton step everywhere the client uses them (renormalisations, soft gammas, clip divisions, segment solves); exact divss for prepare-time 3x3 inverses. Hardware estimates differ between CPUs, so bit-exactness is per machine |

### 6c. The 3.3.5a glue (measured live 2026-10-11)

| Rule | Why |
|---|---|
| A 3.3.5a bone matrix maps a model-bind point into the scene's CAMERA-RELATIVE space; `scene(CM2Model + 0x28) + 0xC4` takes that space to the world and is a MIRROR (det -1). NEVER decompose it as a placement | the first port took it as the placement: retail's "det < 0 -> negate the third axis" broke its inverse and every simulated bone was written reflected through the camera (cam z +11.96 -> -12.05): sheets stretched from the wearer past the camera, "tearing left and right" |
| The instance placement is the MODEL'S OWN matrix `CM2Model + 0xF4` (4x4 row-vector, camera-relative, model scale included) times the scene matrix | live: equals an unanimated bone 0 exactly; scale 0.9775 shoulders, 1.0656 belt, 1.15 helm on a Zandalari; without it items were simulated at scale 1 |
| Write back: retail's model-space bone x `CM2Model + 0xF4` (row vectors); every bone under a dynamic bone carried by its parent's delta | the client draws camera-relative bones |
| Frame time = the scene's clock (`scene + 0x0C`, ms, CM2Scene::AdvanceTime 0x0081C9C0); a second build in one frame takes 0 s | a clock read mid-render disagrees with the game's movement |
| Hook right after CM2Model::BuildBoneMatrices (0x0082F0F0); state per model instance, forgotten when unseen 2 s | |
| A shape-less dynamic body has mass 0 and moves only by its head's teleport share (carry): a pair like the Amani Loa off-hand's bones 1 -> 3 drifts by half the wearer's travel - in retail too; no vertex uses those bones | measured on all 6 files; not a fault |

### 6d. Proof (how it was verified; do the same for any port)

1. ORACLE: run retail's own functions in unicorn on the decrypted __TEXT
   (stack guard pointer at 0x1036F83F8 -> a zero qword; stubs: ___chkstk_darwin
   0x103259882 = return, memcpy 0x103259D2C, __stack_chk_fail 0x100001A40 =
   stop) with crafted inputs (a hull record is 0x50 bytes: centroid, pointers
   to vertices / planes / face edges / half-edges, counts, volume, area,
   radius), against the port compiled with exact estimates
   (PHYS_EXACT_ESTIMATES: an emulator computes rsqrt/rcp exactly).
   Results: hull vs hull 8,200 random poses of all 41 retail hulls - 0
   mismatches, worst 2.4e-7; hull vs capsule 20,500 - same contact count and
   feature keys everywhere, 5 near-parallel edge-edge cases (0.025 %) differ
   by <= 0.1 deg of normal / 3 cm of point inside GJK's last digits (GJK
   ported as b2Distance-3D, not instruction by instruction).
2. WHOLE SIMULATION on every shipped file (1,368) as the live glue drives it
   (mirrored camera matrix, model scale in the bones): stand, walk, 180 deg
   turn, camera orbit - nothing detaches; at rest a simulated bone moves
   median 0.04 mm a frame; orbiting the camera vs not changes results only
   by float rounding of world coordinates (<= 2.8 mm/frame on a swinging
   chain).
3. The user in game: plumes droop and swing, ribbons and chains follow
   walking, running and jumping; turning the camera moves nothing.

Never decompose the scene matrix; never simulate at scale 1 when the bones
carry a scale; never add rules the client does not have (sleep, inertia
bounds, damping relative to the wearer were ours and are gone); prove every
routine against the client's own code before shipping.

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
