# The pipeline, step by step

Every command is `python <skill>/scripts/bp.py <command> --work W ...`. Re-read
the step here before you run it. A step either prints its PASS line or exactly
what is wrong; when the output is something this file does not describe, STOP
and report it to the user with the exact text. Every step can be re-run: it
continues where it stopped and never redoes finished work (except `--force`).

## 0. Setup and the project folder

```
python --version                        # 3.11+
python -m pip install numpy pillow      # the only dependencies
python <skill>/scripts/bp.py selftest   # OK = the kit works on this machine
```
One project folder per port (e.g. `C:\backport\midnight-plate`). It holds:

| Path | What | Written by |
|---|---|---|
| `bp.json` | settings (the only file you edit) | init |
| `db2/<build>/*.csv` | retail tables | any step, on demand |
| `cache/<build>/<fdid>` | retail files (+ names) | fetch, build on demand |
| `listfile.csv` | community listfile (fdid;path, ~150 MB, once) | first step that needs names |
| `client/scan.json`, `client/dbc/*.dbc` | what the client is, its winning tables | scan |
| `client/helm_shift.json`, `client/geoset_map.json` | this client's measured fit | measure |
| `selection.json` | what you asked for, resolved | select |
| `build/` + `build/manifest.json` + `build/plan.json` | converted files, what each is, per display row | build |
| `ids.json` | retail display -> new ids (stable) | tables |
| `out/` | THE RESULT | tables, pack |
| `reports/*.json` | fetch, newtech, audit, tables, physics findings | each step |
| `sheets/*.png` | pictures | sheet |
| `logs/<command>.log` | every run's output | every step |

Settings in `bp.json` (defaults in brackets): `build` (newest live retail),
`client_data`, `locale` [first found], `wildcard_patches` [false],
`namespace` [Backport], `icon_prefix` [bp], `patch_name` [auto],
`display_id_start` / `item_id_start` / `helmvis_id_start` [auto / 900000 /
auto], `server` [azerothcore], `race_map` [{}], `cache` and `listfile` (reuse
existing ones), `workers` [6], `max_texture` [1024 - never raise it].

## 1. init

`init --work W --client "<WoW>\Data" [--build B] [--namespace N] [--server trinitycore] [--cache DIR] [--listfile FILE]`
- Pins `build` to the newest live retail build (wago.tools "wow" product)
  unless given. NEVER change it in a running project (rule 2): start a new
  project for a new build.
- `--cache`/`--listfile` reuse files you already have (same layout:
  `<cache>\<fdid>`, `fdid;path`).
- PASS: prints every setting. FAIL: the client folder has no common.MPQ.

## 2. scan

Reads the client exactly as wow.exe mounts it (references/bodies-and-races.md
section 1). PASS: the mounted archives, the six tables with their source
archive, a race table, `output archive: patch-?.MPQ`. Read the race table:
every PLAYABLE race needs a retail match or a `race_map` entry (else its
helms are invisible). `UNEXPECTED table layout` = not a 3.3.5a 12340 client:
STOP.

## 3. newtech --learn (once per retail build)

Records what retail uses at this build (from your cache) into
`reports/known_tech.json`, merged with the registry the kit ships
(`scripts/data/known_tech.json`, 12.1.0.69933). Run it after the first fetch
too if your cache was empty (it learns from what is cached).

## 4. find

`find --work W "text"`: sets (id, ExpansionID, ClassMask, parent) and items
(id, ExpansionID, quality, InventoryType) whose names contain the text.

## 5. select

Builds `selection.json`: every display (one retail look) the request needs,
with every file it names for every retail race. Inputs (combine freely,
repeat any):

| Option | Means |
|---|---|
| `--set ID` | one TransmogSet (one colourway) |
| `--set ID --family` | the set and every colourway of its family (parent + children) |
| `--item ID` | every look (ItemModifiedAppearance) of one retail item |
| `--appearance ID` | one ItemAppearance |
| `--display ID` | one ItemDisplayInfo |
| `--expansion ID` | every transmog set of the expansion + its weapons, shields and held off-hands |
| `--sets-only` / `--weapons-only` | with --expansion |
| `--min-quality Q` | with --expansion: weapons of quality >= Q (2 green, 3 blue, 4 epic) |
| `--slots a,b` | keep only these slots (head, shoulder, chest, waist, legs, feet, wrist, hands, back, shirt, tabard, mainhand, offhand, ranged, held) |
| `--armor cloth|leather|mail|plate` | keep only that armour type (weapons stay unless --slots) |
| `--add` | add to the current selection instead of replacing it |

Identical looks are one display: a look shared by several sets or colourways
is converted once. Retail internal sets (`[DNT]`, `[PH]`) are skipped. PASS:
`selection: N sets, M displays {per slot}`.

## 6. fetch

Downloads, in two stages, every file the rows name (only the races the client
needs + their fallback chains + race 0) and everything those models name
(skins, textures, .anim, .skel, .phys, particle models), recursively. Six
parallel downloads, each retried 6 times with back-off, then every failure
again one at a time (10 tries, long timeouts); written to `.part` then
renamed. PASS: `fetch: 0 failed`. `missing` = the build has no such file
(reported); `encrypted` = unreleased content (its displays are not shipped,
reported).

When files still fail, in this order (never accept a failed file, never use
another build):
1. Run `fetch` again (it resumes; the ledger stays unticked until it passes).
2. Lower the load: `bp.json` "workers": 2, run again.
3. Check wago is up: open `https://wago.tools/api/builds` in a browser. If it
   is down, wait and run again later.
4. Another source with the SAME build: a local retail install at that build +
   wow.export (export by file id, raw) - save each file as `<cache>/<fdid>`
   (no extension), then run `fetch` again; it uses what is in the cache.
5. Still failing: report the ids (`reports/fetch.json`) to the user. Their
   displays cannot be built correctly - never build them with a gap.

## 7. newtech

Scans every file the selection uses against the registry. PASS: nothing new.
Anything NEW: uncover and support it before building (references/new-retail-tech.md). Never skip this step: it
is how a new expansion's new technology gets caught before it breaks items.

## 8. measure (once per client; again if the client's bodies change)

Writes `client/helm_shift.json` and `client/geoset_map.json`
(references/bodies-and-races.md sections 4 and 5). PASS: every playable body
`measured` with a spread of a few cm. `estimated`/`UNKNOWN` on a playable
body: build anyway, then check that body's sheet first.

## 9. build

`build --work W [--display ID ...] [--force] [--extension]`. For every
display: the models, textures, regions, icon and the row values, by the rules
in references/rules.md. Resumable (a built display is skipped; `--force`
rebuilds; an unchanged converted file is reused). PASS: `N built, 0 errors`
(Encrypted errors are expected and reported). Every other error is grouped
by cause: fix the cause (references/troubleshooting.md), then `build --force
--display <id>` for those displays. The line `N displays have retail 3D parts
a stock client cannot show` is information for the user, not an error.
`--extension` also writes the Tier 2 export (references/client-extension.md).

## 10. tables

Appends rows to the client's three tables (ids stable), writes the server SQL.
PASS: `tables: N displays, N items, H helm-vis rows appended`. Read the report
lines (subclass mappings, ItemVisual not carried) and pass them on.

## 11. audit

Every gate, over exactly what ships (references/troubleshooting.md "Gate
failures"). PASS: `audit: PASS (0 problems)`. Notes (stock-client limits,
estimated shifts, displays not shipped) are information to report.

## 12. prove-gates

Copies the project, plants 8 real defects (floating helm, green texture,
missing file, wrong bone palette, shrunken bounds, white batch, external
sequence 0, a bone flying 10 m) into files that ship, audits the copy and
requires every defect to be caught. PASS: `every planted defect was caught`.
A MISSED gate means the audit cannot be trusted: fix the gate first.

## 13. pack

Writes `out/<patch>.MPQ` (+ `out/extension/<patch>-ext.MPQ` with Tier 2),
reads every file back, checks every art path is private, writes
`out/INSTALL.txt`. Refuses to run on a failing audit. > 2 GB: split the
selection (recipes below).

## 14. sheet

Pictures in `W/sheets/` (`--display ID` for some): every helm on every playable
body (front + three-quarter, body grey, helm textured), shoulders and weapons
textured from two sides, and every body texture. OPEN EVERY PICTURE. Look for:
helm floating above or sunk into the head, helm too big/small, white/black/
green areas, transparent holes, missing parts, red-bordered tiles (the sheet
could not draw something - read its label). Describe what you saw.

## 15. hand over

Give the user `out/INSTALL.txt`, the audit notes (what a stock client cannot
show; estimated shifts; not-shipped displays with why), and the in-game
checklist (references/troubleshooting.md). Say "built and gated offline" until
they confirm in game.

## 16. physics study (optional)

`phys --work W <model fdid>`: readable dump of a model's retail physics.
`--settle`: its rest pose with contacts, joint error, settle time.
`--survey`: every physics model of the selection -> `reports/physics.json`.
Shipping baked rest poses is OFF (references/rules.md section 6).

---

## Recipes

Every recipe = select, then steps 6..15 unchanged. One project per client
(its tables are the client's tables plus ALL our rows).

### A whole expansion (every set and every weapon), organised and complete

1. Find the expansion id: `find --work W "<a set name of that expansion>"` -
   the `exp` column (retail ExpansionID: 9 Dragonflight, 10 The War Within,
   11 Midnight, ...).
2. Select it: `select --work W --expansion 11` prints the displays per slot
   (Midnight at 12.1.0.69933: 515 sets, 3,699 displays: every set, every
   colourway, every weapon/shield/off-hand). ONE project holds it all: `pack`
   splits the art over as many archives as needed (each < 2 GB, art beyond
   1.5 GB uncompressed goes to the next archive on the lowest free patch
   letters) and keeps the three tables in the top archive only - so every
   item of the expansion is in one set of tables with one id range.
   Optional narrower ports of the same expansion, each in its own project:
   `--sets-only --armor plate`, `--weapons-only --min-quality 3`,
   `--sets-only --slots back,tabard,shirt`. Two projects shipped to the same
   client must use different `patch_name`s and non-overlapping id ranges, and
   only the higher one's tables are read: ship ONE project per client.
   Time and disk: a whole expansion is hours of fetch (wago, ~1-3 GB) and of
   build; every step resumes - run them again after any interruption.
3. Curate if the user wants "only the good ones": tier sets (raid set groups
   with 4 difficulty colourways per class), season sets, PvP elite sets, and
   epic/rare sets with unique models; drop common/uncommon (quality <= 2),
   sets whose appearance list equals a kept set's, and sets whose items are
   all in kept sets. Show the user the list and get approval before fetching.
4. A wardrobe UI (a client mod) listing SETS should list only sets that dress
   the body (a chest or legs piece) and not a set whose pieces another listed
   set already has (PvP rank splits, Aspirant = War Mode copies): measured on
   Midnight, 441 -> 271 listed sets. The pieces stay offered as items.

### Specific sets (with all their colourways)

`find "<set name>"` -> `select --set <id> --family` (one set: drop `--family`).
Several: repeat `--set`. The four difficulty colourways of a tier set are a
family (parent + children).

### One item, one weapon, one look

`select --item <retail item id>` (every look of the item) or
`select --display <ItemDisplayInfo id>` (exactly one look). Weapons work the
same way; their sheath type is converted (rules.md section 7).

### Every weapon of an expansion

`select --expansion <id> --weapons-only --min-quality 3`.

### Only some slots (e.g. helms and shoulders of a set)

`select --set <id> --family --slots head,shoulder`.

### Any other retail model (a doodad, a spell effect, a creature part)

`convert-model --work W <model fdid> [--out <archive path>]`: the same model
rules (no item tables). Place it under your namespace and ship it with your
own tables.
