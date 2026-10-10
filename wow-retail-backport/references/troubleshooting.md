# Troubleshooting: the loop, every gate, every issue met so far

## The debugging loop (every time)

1. **Get the exact evidence.** The item name or display id, the body (race,
   sex, HD or legacy), the animation, the camera. Screenshots and the user's
   words are primary evidence. Find the display in `W/build/plan.json` and its
   files in `W/build/manifest.json` (archive path -> kind, source fdid, report).
2. **Match the symptom** in the tables below. If it matches, CONFIRM the named
   cause is present in THIS file (run the gate on it, look at its sheet)
   before believing it.
3. **No match: root cause first.** Compare against retail's own bytes and
   tables, read the client's behaviour (wow.exe 12340 addresses in rules.md),
   use a control: the same item on another body, the same rule on an item
   that works, retail's file vs ours.
4. **Failing test first** in `scripts/tests/` (one assertion per rule, a tiny
   synthetic model/skin/texture is enough). Watch it fail.
5. **Fix the rule in `scripts/lib/`**, never an output file. Watch the test pass;
   `selftest` must stay OK.
6. **Sweep**: `build --force` (or `--display` for the affected ones), `tables`,
   `audit`, `prove-gates`, `pack`, `sheet`. One parser bug once broke 8,820 helms.
7. **Record the rule** in rules.md with its evidence.
8. **Hand back** to the user for the in-game check: "built and gated offline".

## Gate failures (audit)

| Gate | What failed | Usual cause -> fix |
|---|---|---|
| C1/C2 closure | a file a row or model needs is missing | build error on that display (plan.json "error"), or a file deleted by hand: `build --force --display <id>` |
| M1 check_264 | the model breaks the client's read rules | a converter rule regressed: run selftest; the message names the array |
| M2 unchecked reads | an index wow.exe reads without a bounds check is out of range (crash) | the message names the read (verify_bounds.py lists the instruction): fix the rule that wrote that field |
| M3 bone palette | weighted vertices skin with another bone than their own (floats / turns with the camera) | a submesh filter or retail_m2.skin offsets (+0x0E boneComboIndex vs +0x10 boneInfluences) |
| M4 billboard | a one-bone model whose bone faces the camera | single-bone rewrite must clear bone 0's billboard flags and tracks |
| M5 sequence 0 external | the client reads sequence 0 without an .anim: crash | the model writer lost flag 0x20 on sequence 0 |
| M6 bounds | the box misses the mesh: vanishes on zoom/camera dips | union_bounds / fit_bounds not applied after a transform |
| M7 draws white | a batch samples a slot nothing binds | a retail-only slot (3/4/24) without its colourway texture: check build.Retail.supplies; else the batch must be dropped |
| M8 motion | a bone flies > 3 m, collapses, non-finite, or the model cannot be read for animation | a track or .anim from another build (rule 2), a broken key array, an alias pointing nowhere |
| T1 texture | > 1024, encoding 3, not a power of two, mips outside the file or inside the 1172-byte header | tex335 bypassed or regressed |
| T2 green | a body-region texture is DXT | must be palettised (convert.section_texture) |
| P1 placement | a helm does not reach the head of a client body | the helm shift for that body (`measure`; estimated shifts), a body-space helm not made point-local, a wrong retail match (scan race table) |
| D1 tables | a table missing or with duplicate ids | run `tables`; never hand-edit ids.json |
| newtech NEW ... | retail uses something unmeasured | new-retail-tech.md |
| prove-gates MISSED | a gate did not catch its planted defect | the gate is broken: fix the gate before anything else |

## Every issue met so far (all from real in-game reports, with the fix the kit carries)

| Reported in game | Root cause (measured) | Now handled by |
|---|---|---|
| Helms ~20 cm high/forward on classic bodies | retail cuts are placed for retail's helm point (on the head bone); 3.3.5 bodies' point sits higher and forward | measured helm shift per body (`measure`), gate P1 |
| Helm turning with the camera, floating hood (Kobold hood, Blind Oath crest) | parser swapped skin submesh +0x0E/+0x10: vertices skinned to a flame's spherical billboard bone | retail_m2.skin fixed; gate M3; billboard gate M4 |
| A whole one-bone piece turning with the camera | bone 0 of the source was a glow card's billboard | single_bone clears it; gate M4 |
| Helm a body-height above the head | helm authored on the character's bones hung on attachment 11 | body-space helms made point-local |
| Helm 40 cm low / checker box on a race retail cuts no helm for | fallback to a file placed for another body, or a missing file | invisible stand-in under the body's own name |
| Helms beside the head, plumes spinning (after a physics bake) | proven: the offline settle had no contacts (parts fell through the wearer stand-ins); unproven: whether retail droops free chains as far as a settle does | bind pose ships; contacts added; a bake ships only after comparison with retail (rules.md section 6) |
| Stiff/pointy tentacles, plumes, chains; ribbons that never move when jumping | retail moves them with runtime physics; 3.3.5 has none | bind pose on a stock client; Tier 2 LIVE physics (client-extension.md section 5, `physlive.py`) |
| Physics parts fly back, whip or go crazy when the wearer RUNS or JUMPS (fine standing) | damping applied to WORLD velocity (a steady run is wind: 70 deg back), the wearer's instant travel changes felt whole (flung 90 deg), or frames timed by a clock read mid-render (the game's own frame time differs: 17 deg of shaking) | the item rides its wearer, bounded inertia, the SCENE's clock, travel measured over 0.2 s with hysteresis along it (rules.md section 6) |
| Physics parts swing when only the CAMERA turns | simulated in the client's camera-relative bone space | simulate in the world (bone x the scene's camera-to-world matrix), rules.md section 6 |
| A plume torn off its socket, links shot away | joined bodies collided: retail authors the first link inside the helm shell | joined bodies never collide |
| Every 3D chest/legs/feet/back/hands piece gone in game, verify said 0 missing | the archive closure followed table rows only; the mod builds per-body file names itself | closure and verify enumerate the mod's runtime names (client-extension.md section 1, item 7) |
| Skin pokes through cloth while running on classic bodies | the posed clearance ran on float weights; the file ships 8-bit weights | clear again on the shipped weights, capped 2 cm (client-extension.md section 1, item 5) |
| Legs vanish under boots, feet cut | the row asks a body for a geoset variant it lacks (506..510, 905, 1105, 1803/1804) | measured geoset map per client |
| Footless footwraps | GeosetGroup[1] = -1 read as "no foot" | -1 = the default foot (2001) - client-mod rule (Tier 2 feet group 20) |
| Black feet on a retail body | the body skin's foot rect empty, or a boot shell mapping outside it | body-port issue (client-extension.md); Tier 1 paints region 7 |
| White rectangles (collars, skirts) | batches on retail-only slots 3/4/24 a slot display does not hand (a combined set model) | colourway completion over all retail displays + drop; gate M7 |
| Flat additive sheet on crowns/shoulders | the item's own (type 2) texture slot was stripped | type 2 kept live; row texture |
| White square on a hood | a 2-unit batch's unit 1 reads UV2 (3.3.5 has no Diffuse_T1_T1) | second_unit_on_uv1 |
| Pale or blown-out glow | material blend 7 mapped to Add | 7 -> 2 (materials), particles their own enum |
| Top-tier/Gladiator glow missing | GeosetGroupOverride ignored | x01 + max(AGG, override) |
| Robe skirt glow twice as bright | chest robe and legs both drew group 13 | Tier 2 rule (legs' 13xx off under a robe chest) |
| Item vanishing on camera dip/zoom/jump | the box missed the mesh (the client culls by the current sequence's box) | union_bounds/fit_bounds; gate M6 |
| Black chest/face in some animations (Goblin) | colour track keyed in sequence 0 only; unkeyed = 0 | constant_colours |
| GREEN textures on items/bodies | DXT textures in compositor slots | palettised sections; gate T2 |
| Random/garbage textures, crashes in texture code | textures > 1024, encoding 3, or mip data inside the BLP2 header (5,761 of one build's 9,323 textures, 18 smaller than the header) | tex335 + gate T1 |
| Invisible underwear/cloth on a retail body | Pillow reads palettised BLP alpha as 0 | the kit's own palette decoder |
| Crash on load (0x00836D11) | raw retail shader ids > 0x8003 (MultiConverter2 output) | shader table mapping; never ship MultiConverter2/M2Mod output |
| Crash on load (0x00828565) | sequence 0 / Stand external | gate M5 |
| Helm/items show the wrong colour on one colourway | per-colourway glow textures / ParticleColor not baked | baked tags per colourway |
| Hair, ears, horns through helms | helm-hide rows missing for that race/vis id | HelmetGeosetVisData rows from retail HelmetGeosetData |
| Empty or naked sets in a wardrobe UI; the same pieces listed as a big set and again as 2-piece sets | accessory-only bundles (totems, cloaks, tabards, weapon arsenals) and PvP rank splits / Aspirant=War Mode copies listed as sets | list only sets with a chest or legs piece and not a subset of a bigger listed set (pipeline.md recipes) |
| Items floating / wrong on another realm's custom client | that client's patches replaced shared files or bodies | private paths only, client scanned before building, never mount or replace anything |
| Model and animation from two retail builds (garbage motion) | wago served the newest copy of a file in any product | one pinned build for everything |
| A helm fixed for one item broke all other helms | a "fix" applied to every helm instead of the one rule | fix rules with a test and sweep with the gates; never per-item hand edits |

## Crash triage (#132)

The `Errors\*.txt` is not the evidence; the `.dmp` beside it is. Parse its
MemoryListStream (type 5): it holds the faulting object. Disassemble the
faulting instruction, walk out to its function, decode saved registers from the
raw stack (the .txt's manual trace skips FPO frames), read the object's
constructor, and compare SIBLING objects of the same class in the dump: two
siblings agreeing with the constructor against the crasher prove a stray
write. Never assume the stack names the culprit. Map the address to a rule in
rules.md (0x00836D11 shader id; 0x00828565 external sequence 0; 0x0047BF7C
multi-texture particle; texture heap corruption = BLP limits).

## In-game checklist (give it to the user, one line each)

For each set or item, on one body of each family the user plays (e.g. Human
M/F, a large body like Tauren, a small one like Gnome) and HD or legacy as they
run:
1. Helm sits on the head; turn the camera all the way round: it does not turn
   with it or float; dip the camera and zoom: nothing vanishes.
2. Hair, ears, horns hidden as retail hides them; no face through visors.
3. Shoulders sit on the shoulders, left and right the right way round.
4. Boots: legs whole below the knee, feet present, no gap.
5. No white, black or green areas; glows present on the top-tier colourway
   and absent on the lower ones.
6. Walk, run, jump, cast, mount: nothing flies, spins or tears. Physics parts:
   with a live-physics mod they droop, swing and follow every move, and stay
   still when only the camera turns; without one they keep retail's authored
   pose.
7. Weapons held the right way round, sheathed on the back/hip per type.
8. Character panel, inspect, character select show the same look.
9. A player WITHOUT the archive sees nothing broken (the server sends ids the
   client then cannot draw: they must install it - INSTALL.txt).
