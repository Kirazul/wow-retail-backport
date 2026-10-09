# New retail technology: catch it, research it, support it

Retail changes every expansion: new model chunks, new flag bits, new texture
types, new blend modes, new BLP formats, new table columns. The kit's rules
were measured on what retail used at 12.1.0.69933. Anything new, converted by
old rules, is the next white sheet, black part or crash. So `newtech` stops the
run first.

## What `newtech` compares

`scripts/data/known_tech.json` (shipped; measured over every cached retail
model, skin, texture, .anim and .skel at 12.1.0.69933, plus the chunk types and
blend values the converter documents) merged with `W/reports/known_tech.json`
(`newtech --learn` on your own cache and build):

| Kind | Known at 12.1.0.69933 |
|---|---|
| m2.chunk | MD21 SFID TXID AFID BFID SKID PFID PFDC TXAC EXP2 EXPT LDV1 RPID GPID PABC PADC PSBC PEDC WFV1-3 PGD1 EDGF NERF DETL DBOC AFRA DPIV |
| m2.version | 272, 274 |
| m2.global_flags, bone_flags, material_flags, texture_flags, particle_flags, sequence_flags | the bits seen (see the json) |
| m2.material_blend, particle_blend | 0..7 |
| m2.texture_type | 0 1 2 3 4 6 7 8 9 10 15 16 17 18 19 20 24 25 26 |
| blp.format (type/encoding/alphaDepth/alphaType) | 1/1/0/8 1/1/8/8 1/2/0/0 1/2/1/0 1/2/4/1 1/2/8/1 1/2/8/7 |
| anim / skel chunks | AFM2 AFSB / SKL1 SKA1 SKB1 SKS1 SKPD AFID BFID |
| db2.<Table> | every column of every table the kit reads |

## When it stops you

1. List what is new (`reports/newtech.json` has examples: file ids).
2. Look at one example: `convert-model` it (it may fail - that is information),
   dump it (`phys` for physics, a hex view for a chunk).
3. Research, in this order: wowdev.wiki (M2, BLP, DB2/<table>), the
   wow.export source (src/js/3D/loaders: M2Loader, SKELLoader, BLP), WoW Model
   Viewer source, the WebWowViewerCpp source. Write down what the feature
   does in retail.
4. Decide what 3.3.5 can do with it - and prove it:
   - **convert** (the client has an equivalent: map it, cite the wow.exe
     address or the paired retail/WotLK files you measured),
   - **bake** (compute its effect offline into data the client reads),
   - **drop with a report** (no equivalent; say so in rules.md as HARD LIMIT and
     make the build report count it),
   - **Tier 2** (a client mod could do it; client-extension.md).
5. Write the rule with a failing test first (`scripts/tests/`), then the code
   (`scripts/lib/`), then the evidence row in rules.md.
6. `newtech --learn` so the registry knows it; `selftest`; continue the pipeline.

A new TABLE COLUMN usually means a new behaviour of an existing system (e.g.
HelmetGeosetData gained RaceBitSelection): find what it does before trusting
the old meaning of the other columns.

## Retail systems and where they stand (keep this table current)

| System | Status |
|---|---|
| Per-race item cuts (ComponentModelFileData) + race fallbacks | DONE |
| Combined set models (one model, several slots) | DONE (slot filters) |
| Retail-only texture slots 3/4/24 | DONE (baked per colourway) |
| ParticleColor per display | DONE (baked) |
| EXP2 particle multipliers | DONE (baked) |
| Particle format flag 0x200 | dropped, reported (HARD LIMIT) |
| Multi-texture particles | layer 0 (HARD LIMIT) |
| Edge fade shaders (EDGF) | dropped (HARD LIMIT) |
| More than 2 texture units | first two (HARD LIMIT) |
| Runtime physics (.phys) | bind pose; rest pose with contacts available offline; runtime = Tier 2 (rules.md section 6) |
| HelmetAnimScaling | not carried |
| SheatheTransformMatrix | not carried |
| ItemVisual kits | not carried |
| Race-specific body textures, section 8 | not carried (3.3.5 regions have no race) |
| Collection 3D pieces, buckles, quivers, 3D capes | Tier 2 |
| New retail races' bodies | Tier 2 (client-extension.md) |
