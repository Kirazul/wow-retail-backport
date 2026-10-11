---
name: wow-retail-backport
description: Use when backporting, porting or converting anything from retail World of Warcraft (any expansion - transmog sets, armour, helms, shoulders, cloaks, weapons, shields, off-hands, single items, a whole expansion, or any retail model) to a WotLK 3.3.5a (build 12340) client - stock, HD-patched or a custom realm client, any race - or when a backported item is wrong in game - floating or sunk helm, helm turning with the camera, item vanishing on zoom, white sheets, green or black textures, missing glows, legs or feet cut, broken or frozen animation, parts flying off, checker boxes, crashes on load.
---

# Retail -> 3.3.5a backport (any item, any client, any race)

## In plain words

Retail WoW describes an item with DATABASE TABLES: which model file each race
and sex wears, which textures paint it, which body parts (geosets) show,
which hair a helm hides. Its files are in newer formats. A 3.3.5a client reads
older formats and older tables.

This skill ships a complete, tested toolkit (`scripts/bp.py`) that:
1. reads retail's own tables and files at ONE pinned build (wago.tools),
2. reads the TARGET client's own files (any 3.3.5a client: its races, its
   body models, its tables, its patches),
3. converts every file with measured rules (each rule has evidence and a test),
4. MEASURES how that client's bodies differ from retail's (helm placement,
   body geosets) and fits every item to them,
5. writes one patch MPQ + the client/server tables + the server SQL,
6. GATES everything (crash, white, green, black, floating, vanishing, flying
   parts, missing files) and draws pictures you must look at.

You run numbered commands. Each prints PASS or exactly what is wrong.
You never edit an output file by hand and never guess a rule.

## The rules you never break

1. **Re-read this skill's row for a step (and the reference it names) BEFORE
   running the step.** Every time. Do not work from memory.
2. **One pinned retail build** for tables AND files (`bp.json` "build"). Never
   mix builds: a model and its .anim from two builds play garbage.
3. **Never invent a rule.** A rule cites a retail table field, a wow.exe
   12340 address, wowdev.wiki, or a measurement (script + numbers). If a
   step needs a rule you cannot cite: STOP, measure it, or ask the user.
4. **Never override the game.** Every file we add lives under the private
   namespace folder (`Backport\` by default); ids are new ids above the
   client's; tables only get APPENDED rows. The only shared names are the
   three DBFilesClient tables, rebuilt from the client's own winning copies.
5. **A gate failure is fixed in the CONVERTER (scripts/lib), never in an output
   file**: add a test, fix the rule, rebuild, re-audit. One parser bug once
   broke 8,820 helms; patching outputs would have hidden it.
6. **The user's in-game check is the only final proof.** Say "built and gated
   offline" until the user confirms in game. Offline gates passed while the
   game was broken more than once.
7. **Look at every sheet picture** before saying anything looks right. Never
   claim you looked when you did not.
8. **When `newtech` finds something new, uncover it fully - never skip it.**
   The build pauses so nothing is converted wrong; your job is then to find
   out exactly what the new retail feature does and support it (convert, bake,
   or a reported hard limit with its measured reason) with a rule + a test,
   then continue (references/new-retail-tech.md). Never drop it silently,
   never build around it, never give up on an issue: every issue gets solved.
9. Do not deploy, publish or touch the user's game or server unasked. Hand
   over `out/` and `out/INSTALL.txt`.
10. Heavy work one step at a time; every step is resumable - re-running it
    continues where it stopped.

## Setup (once per machine)

```
python --version                      # 3.11 or newer
python -m pip install numpy pillow    # Pillow 11+ (DDS encoder); nothing else is needed
python <skill>/scripts/bp.py selftest # must end with OK (27 tests; "skipped=2" is fine: those need retail files)
```
`<skill>` is this skill's folder. `W` below is the project folder you choose
(outside any git repo, e.g. `C:\backport\midnight`). Commands work in
PowerShell, cmd and bash alike. Put paths with spaces in double quotes.

## How every step is verified (the ticking ledger)

Every pipeline step records PASS or FAIL in `W/reports/steps.json`. A step
REFUSES to run (exit 3, `REFUSED: step 'x' needs PASS first on: ...`) until
every step it depends on has passed, and re-running a step un-ticks every
step after it - nothing can be skipped, nothing stale can ship. After EVERY
step:
1. read its last lines: the PASS line in the table below must be there;
2. run `python bp.py status --work W` and see its box ticked `[x]`;
3. only then move to the next row. A `[!]` (FAIL) or `REFUSED`: fix it first
   ("When a step fails") - never work around the ledger.

## The pipeline (run in order; each row: re-read it first)

| # | Command (`python <skill>/scripts/bp.py ...`) | What it does | PASS looks like | If not |
|---|---|---|---|---|
| 1 | `init --work W --client "<WoW>\Data"` | creates `W/bp.json`, pins the newest live retail build | prints the settings | client path must hold common.MPQ |
| 2 | `scan --work W` | reads the client: archives in the client's own priority, races + prefixes + playable flag, body models, table layouts, free ids, output archive name | a race table + `scan: PASS` | a playable race with no retail match: set `race_map` (bodies-and-races.md section 2) and scan again |
| 3 | `newtech --work W --learn` | OPTIONAL: the kit ships a registry measured at 12.1.0.69933; on a NEWER build run this after the first fetch so the registry also knows what your cache shows | `recorded N feature kinds` | - |
| 4 | `find --work W "name"` | search retail sets and items by name | ids listed | try a shorter word |
| 5 | `select --work W --set ID [--family] --item ID --display ID --expansion ID` | resolves what to port to displays + every file they name | `selection: N sets, M displays {...}` | an unknown id: check it with `find` |
| 6 | `fetch --work W` | downloads every needed retail file (resumable; failures retried one by one) | `0 failed` | run again; then the fallbacks in pipeline.md step 6 (fewer workers, wait for wago, wow.export from a retail install at the SAME build); encrypted = unreleased, reported, not shipped |
| 7 | `newtech --work W` | stops on anything retail uses that the kit never measured | `PASS - nothing ... is new` | uncover and support it: references/new-retail-tech.md |
| 8 | `measure --work W` | helm placement per body + body geoset remap, for THIS client | every playable body `measured` | `estimated`/`UNKNOWN` bodies: look at their sheets, bodies-and-races.md |
| 9 | `build --work W [--extension]` | converts everything (Tier 1; `--extension` also exports Tier 2 pieces) | `N built, 0 errors` | errors grouped by cause: troubleshooting.md |
| 10 | `tables --work W` | DBC rows (client + server) + item_template SQL, stable ids | `tables: N displays, N items` | - |
| 11 | `audit --work W` | every gate (closure, crash, white, green, bones, bounds, motion, placement, tables) | `audit: PASS (0 problems)` | troubleshooting.md "Gate failures" |
| 12 | `prove-gates --work W` | plants 8 real defects in a copy: the audit must catch every one | `PASS - every planted defect was caught` | a MISSED gate is broken: fix it before trusting any audit |
| 13 | `pack --work W` | `out/<patch>.MPQ` (more archives automatically when the art passes 1.5 GB; tables only in the top one) + read-back + privacy + `INSTALL.txt` | `read-back identical` on every archive | REFUSED: audit and prove-gates must pass first |
| 14 | `sheet --work W` | pictures in `W/sheets/`: helm on every playable body, models, textures | `N pictures` - then OPEN EVERY ONE | anything floating/sunk/white/black/green/torn: troubleshooting.md |
| 15 | hand over | give the user `out/INSTALL.txt` + the in-game checklist (troubleshooting.md) | user confirms in game | the debugging loop (troubleshooting.md) |

`python bp.py status --work W` prints where the project stands and the one
next command. `convert-model --work W <fdid>` converts any single retail model
(a doodad, an effect) with the same rules. `phys --work W <fdid> [--settle]`
dumps and simulates a model's retail physics; `phys --work W --survey` does it
for every physics model of the selection (rules.md section 6). Live physics in
a client mod runs retail's own engine, `scripts/ref/physics_domino.cpp`
(client-extension.md section 5, rules.md section 6).

## Recipes (what to put in step 5; full detail: references/pipeline.md "Recipes")

| Goal | select |
|---|---|
| A WHOLE expansion: every set, every colourway, every weapon/shield/off-hand | `--expansion <ExpansionID>` (Midnight = 11: 515 sets, 3,699 displays). One project; `pack` splits the art over several archives automatically (each < 2 GB) and keeps the tables in the top one |
| Only the sets of an expansion / only its weapons | `--expansion <id> --sets-only` / `--expansion <id> --weapons-only --min-quality 3` |
| One armour type of an expansion | `--expansion <id> --sets-only --armor plate` |
| One set with all its colourways | `--set <id> --family` (one colourway: drop `--family`) |
| One item (every look) / one exact look | `--item <id>` / `--display <id>` |
| One weapon | `--item <weapon item id>` |
| Some slots only | add `--slots head,shoulder` |
| Several things | repeat options, or `--add` to extend the current selection |
Find ids with `find --work W "name"` (the `exp` column is the ExpansionID).
For "only the good sets" of an expansion, curate with the user first
(pipeline.md "Recipes", step 3) - show the list, get approval, then fetch.
Ship ONE project per client: its tables are the client's own plus all our rows.

## What a stock client can show (Tier 1) and what needs a client mod (Tier 2)

| Slot | Tier 1 (any 3.3.5a client, this kit's archive + tables) | Tier 2 (needs a client mod; `build --extension`) |
|---|---|---|
| Head | retail's own per-race helm, placed for each client body (measured shift); hides hair/ears/facial as retail does; invisible stand-in where retail cuts none | helms on new retail-race bodies; live physics (plumes, tentacles) |
| Shoulders | retail models (race-agnostic), left/right, enhanced variant on top-tier looks | live physics (ribbons, chains) |
| Weapons, shields, off-hands | retail models, particles, glows, sheath type | retail sheath transform |
| Chest, legs, feet, hands, wrists, waist, shirt, tabard | the 8 body textures (palettised), body geosets remapped to this client's bodies | retail 3D pieces (collars, skirts, boot shells, gauntlets, belt pieces), belt buckles |
| Back | the cape texture + cape geoset | retail 3D capes, quivers |

Tier 2 needs a client extension that binds skinned pieces to the body's bones
(references/client-extension.md gives the exact contract). The audit lists,
per display, what a stock client cannot show - tell the user.

## When a step fails

- Read the printed reason. Find it in references/troubleshooting.md.
- Root cause first (REQUIRED SUB-SKILL: systematic-debugging if available).
- Write a failing test in `scripts/tests/` that reproduces it, fix the rule in
  `scripts/lib/`, run `selftest`, then `build --force`, `tables`, `audit`,
  `prove-gates`, `pack`, `sheet`.
- Never delete a gate or loosen a threshold to make a build pass. If a gate
  is wrong, prove it wrong with a measurement and record that in rules.md.

## When newtech finds something new

The build waits; the work does not. For each NEW item: find the files it
named, take one apart (`convert-model`, `phys`, a hex view), read wowdev.wiki
(M2 / BLP / DB2 / PHYS pages) and the wow.export or WoW Model Viewer source
for that feature, and MEASURE what it does on real files. Then support it -
convert it, bake it, or (only when 3.3.5 truly has nothing equivalent) drop it
with a measured reason in rules.md and a line in the build report - with a
failing test first, then the code, then `newtech --learn`, `selftest`, and the
pipeline again from step 7. Full procedure: references/new-retail-tech.md.

## Symptom -> cause -> where it is handled

| In game | Cause | Handled by |
|---|---|---|
| Helm 15-30 cm high / forward | retail cut placed for retail's helm point | `measure` helm shift, gate P1 |
| Helm turns with the camera / floats away | vertex skinned to a billboard bone (palette read wrong) | m2edit palette rule, gate M3/M4 |
| Item vanishes when the camera dips or zooms | bounding box misses the mesh | union_bounds, gate M6 |
| Flat white sheet / white square | batch samples a texture slot nothing binds; 2-unit batch reading UV2 | slot baking + drop, second_unit_on_uv1, gate M7 |
| Green body texture | DXT texture in a compositor (TextureComponents) slot | palettised sections, gate T2 |
| Garbage / random texture, crash on a small texture | mip data inside the 1172-byte BLP2 header | tex335 keeps the full header, gate T1 |
| Black part in some animations | colour track keyed in one sequence only | constant_colours |
| Legs vanish under boots, missing feet | geoset variant the client body lacks | `measure` geoset map |
| Glow missing on the top-tier look | GeosetGroupOverride ignored | attachment rule (rules.md) |
| Parts fly / spin off | bad bone track | gate M8 motion |
| Plumes/ribbons stand stiff, never swing | retail moves them with runtime physics | Tier 2 live physics (client-extension.md section 5); stock: bind pose |
| Physics cloth moves when the camera turns | simulated in camera-relative space | simulate in the world (rules.md section 6) |
| 3D pieces all gone, verify said 0 missing | runtime-named per-body files not packed | client-extension.md section 1, item 7 |
| Checker box | missing model file | closure gate C1/C2, invisible stand-ins |
| Crash on load | seq 0 external, > 1024 px texture, BLP encoding 3, shader id > 0x8003, unchecked index | gates M1/M2/M5/T1 |
| Hair through the helm | helm-vis rows missing | tables HelmetGeosetVisData |
| Wrong colour on one colourway | per-colourway glow/ParticleColor not baked | baked tags (rules.md) |

Full list with every issue ever met and its fix: references/troubleshooting.md.

## References (read the one a step names, when it names it)

- `references/pipeline.md` - every step: reads, writes, output, failures, resume, files of a project.
- `references/rules.md` - every conversion rule with its evidence: models, animation, materials/shaders, textures, particles, ribbons, physics, geosets, helms, sheath, icons, tables.
- `references/bodies-and-races.md` - how the kit fits ANY client's races: matching, fallback chains, helm shift, geoset map, custom and NPC races, HD vs legacy bodies.
- `references/delivery.md` - archive name and priority, DBC layouts, ids, SQL, install, rollback, coexisting with other patches.
- `references/troubleshooting.md` - the debugging loop, every gate failure, every in-game symptom met so far with root cause, crash triage, the in-game checklist.
- `references/new-retail-tech.md` - what `newtech` watches and how to add support for a new retail feature.
- `references/client-extension.md` - Tier 2: what a client mod must do to show retail 3D pieces, buckles, new races, physics.
