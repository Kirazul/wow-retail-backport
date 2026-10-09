# wow-retail-backport

**An Agent Skill + Python toolkit that backports any retail World of Warcraft item, transmog set,
weapon or whole expansion to a WotLK 3.3.5a (build 12340) client - fitted to every race of that
client, with measured rules and self-verifying gates.**

Give it to Claude Code, Codex, OpenCode, Copilot or any agent that reads Agent Skills, point it at
your 3.3.5a client, name what you want ("every Midnight set", "Hallowfall Plate Armor with all its
colourways", "this one weapon"), and it runs a fixed, verified pipeline: retail tables and files in,
a patch MPQ + client/server tables + server SQL out.

---

## Contents

- [What it does](#what-it-does)
- [What a stock client can show](#what-a-stock-client-can-show-tier-1-vs-tier-2)
- [Install the skill](#install-the-skill)
- [Quick start](#quick-start)
- [The pipeline](#the-pipeline)
- [Recipes](#recipes)
- [How it fits any client and any race](#how-it-fits-any-client-and-any-race)
- [How it verifies itself](#how-it-verifies-itself)
- [Physics](#physics)
- [Repository layout](#repository-layout)
- [Credits and data sources](#credits-and-data-sources)
- [Legal](#legal)

---

## What it does

Retail WoW describes an item with database tables (which model each race and sex wears, which
textures paint it, which body parts show, which hair a helm hides) and stores it in file formats a
3.3.5a client cannot read. This kit:

1. **Reads retail** - its DB2 tables and files at ONE pinned build, from [wago.tools](https://wago.tools).
2. **Reads your client** - its archives in the client's own priority, its races and body models,
   its tables, its free ids, its other patches. Stock, HD-patched or a custom realm client.
3. **Converts** every model, animation, texture, particle and table row with measured rules
   (each rule cites a retail table field, a `wow.exe` 12340 address or a measurement, and has a test).
4. **Measures your client's bodies** (where each race wears a helm, which body geosets exist) and
   fits every item to them.
5. **Writes the result** - one patch MPQ (split automatically when large), the three extended client
   tables (also for the server), and `item_template` SQL for AzerothCore or TrinityCore.
6. **Gates everything** - crash, white, green, black, floating, vanishing, flying parts, missing
   files - and draws contact sheets you must look at.

Everything you add lives under a private folder and new ids above the client's own: nothing of the
client, the server or another patch is ever replaced.

## What a stock client can show (Tier 1 vs Tier 2)

| Slot | Tier 1 - any 3.3.5a client (this kit's output) | Tier 2 - needs a client mod |
|---|---|---|
| Head | retail's per-race helm for every client race, placed for that client's bodies; hair/ears/facial hidden as retail hides them | helms on new retail-race bodies |
| Shoulders | retail models (left/right), top-tier enhanced variants | - |
| Weapons, shields, off-hands | retail models, particles, glows, sheath types | retail sheath transform |
| Chest, legs, feet, hands, wrists, waist, shirt, tabard | the 8 body textures + body geosets remapped to your client's bodies | retail 3D pieces (collars, skirts, boot shells, gauntlets), belt buckles |
| Back | cape texture + cape geoset | 3D capes, quivers |

`build --extension` exports the Tier 2 pieces and a manifest; `references/client-extension.md`
is the exact contract a client mod must meet.

## Install the skill

Copy the folder `wow-retail-backport/` (keep its name) into your agent's skills folder:

| Agent | Folder |
|---|---|
| Claude Code | `~/.claude/skills/wow-retail-backport` (Windows: `%USERPROFILE%\.claude\skills\...`) or `<project>/.claude/skills/...` |
| Codex CLI | `~/.agents/skills/wow-retail-backport` (also `~/.codex/skills`) |
| OpenCode | `~/.config/opencode/skills/wow-retail-backport` |
| GitHub Copilot CLI | `~/.copilot/skills/wow-retail-backport` |
| Other agents | their Agent Skills folder; or tell the agent to read `wow-retail-backport/SKILL.md` and follow it |

Requirements: **Python 3.11+**, `python -m pip install numpy pillow`, internet access to wago.tools
and github.com. Check the kit:

```
python wow-retail-backport/scripts/bp.py selftest      # must end with OK
```

## Quick start

Ask your agent, for example:

> Backport the retail set "Hallowfall Plate Armor" with all its colourways to my 3.3.5a client at
> `C:\WoW\Data`.

Or run it yourself (`W` = a project folder of your choice):

```
python bp.py init    --work W --client "C:\WoW\Data"
python bp.py scan    --work W
python bp.py find    --work W "Hallowfall"
python bp.py select  --work W --set 3655 --family
python bp.py fetch   --work W
python bp.py newtech --work W
python bp.py measure --work W
python bp.py build   --work W
python bp.py tables  --work W
python bp.py audit   --work W
python bp.py prove-gates --work W
python bp.py pack    --work W
python bp.py sheet   --work W        # then LOOK at every picture in W/sheets
python bp.py status  --work W        # the ticked checklist and the next command
```

The result is in `W/out/`: `patch-<X>.MPQ` (+ more parts if large), `dbc/`, `sql/item_template.sql`
and `INSTALL.txt` with the exact player and server-admin steps.

## The pipeline

| # | Command | What it does |
|---|---|---|
| 1 | `init` | creates the project, pins the newest live retail build |
| 2 | `scan` | reads the client: archive priority, races, prefixes, playable flag, bodies, tables, free ids, output archive name |
| 3 | `newtech --learn` | (optional, new builds) records what retail uses at this build |
| 4 | `find` | searches retail sets and items by name |
| 5 | `select` | resolves sets / items / looks / an expansion into displays and every file they need |
| 6 | `fetch` | downloads every needed retail file (resumable, failures retried) |
| 7 | `newtech` | STOPS if retail uses anything the kit has not measured |
| 8 | `measure` | measures this client: helm placement per body, body geoset remap |
| 9 | `build` | converts everything (`--extension` also exports Tier 2) |
| 10 | `tables` | appends client/server table rows, writes SQL, stable ids |
| 11 | `audit` | every gate; exit 1 on any problem |
| 12 | `prove-gates` | plants 8 real defects in a copy; the audit must catch all |
| 13 | `pack` | archives + byte-for-byte read-back + privacy check + INSTALL.txt |
| 14 | `sheet` | pictures: every helm on every playable body, models, textures |

Extra: `status` (checklist + next step), `phys` (retail physics dump / rest-pose simulation),
`convert-model` (any single retail model: doodads, effects).

## Recipes

| Goal | `select` |
|---|---|
| A whole expansion (every set, colourway, weapon) | `--expansion 11` (Midnight: 515 sets, 3,699 looks) |
| Only its sets / only its weapons | `--expansion 11 --sets-only` / `--weapons-only --min-quality 3` |
| One armour type | `--expansion 11 --sets-only --armor plate` |
| One set with all colourways | `--set <id> --family` |
| One item / one exact look | `--item <id>` / `--display <id>` |
| Some slots only | add `--slots head,shoulder` |

## How it fits any client and any race

Nothing about races or bodies is hard-coded:

- **Races** come from the client's own `ChrRaces.dbc` (columns located by content, so custom layouts
  work), matched to retail by id and prefix; custom races map with `race_map` in `bp.json`.
- **Which helm a body wears**: retail's own cut for that race, else retail's fallback chain, else an
  invisible stand-in (never a checker box).
- **Helm placement**: measured per body from helm files your client and retail both ship (one
  constant per race/sex; e.g. legacy Human male 6 cm forward, 18 cm up). Without pairs, estimated
  from the head bone and flagged for review.
- **Body geosets**: each retail variant your bodies lack (boots 506..510, kneepads 905, ...) becomes
  the closest shape your bodies have, measured mesh to mesh.

## How it verifies itself

- **A ticking ledger**: every step records PASS/FAIL; a step refuses to run until the steps it
  depends on have passed, and re-running a step un-ticks everything after it.
- **Gates** (`audit`): closure (every file exists), `check_264` (every array and index the client
  reads), the unchecked-read crash sites of `wow.exe`, bone palettes (no vertex on a billboard bone),
  bounding boxes (nothing vanishes), white batches, sequence 0 resident, motion sampling (nothing
  flies off), texture limits (size, encoding, header), green-texture rule, helm placement on every
  body, table integrity.
- **`prove-gates`**: plants a floating helm, a green texture, a missing file, a wrong bone palette,
  shrunken bounds, a white batch, an external sequence 0 and a bone flying 10 m into a copy of your
  build - the audit must catch every one.
- **`newtech`**: compares every retail file against a registry of what the kit has measured; a new
  chunk, flag, blend mode, texture type or table column stops the run before it is converted wrong.
- **27 unit tests** (`selftest`), pinning rules that once broke items in game.

## Physics

Retail moves capes, plumes and chains with a runtime physics engine (`.phys`); a 3.3.5a client has
none. The kit reads `.phys` completely (`phys <fdid>` dumps bodies, shapes, joints, motors) and
includes a tested XPBD solver with contacts against the wearer stand-in shapes the file carries.
Models ship in retail's authored bind pose; a simulated rest pose ships only after it has been
compared with retail footage. True 1:1 motion needs runtime physics in a client mod (contract in
`references/client-extension.md`).

## Repository layout

```
README.md
wow-retail-backport/              the skill (copy this folder)
  SKILL.md                        what an agent reads first: rules, pipeline, recipes, symptoms
  references/
    pipeline.md                   every step in detail + recipes
    rules.md                      every conversion rule with its evidence
    bodies-and-races.md           fitting any client's races and bodies
    delivery.md                   archives, tables, ids, SQL, install, coexistence
    troubleshooting.md            debugging loop, gate failures, every issue met, crash triage, in-game checklist
    new-retail-tech.md            catching and supporting new retail technology
    client-extension.md           Tier 2 contract for client mods (3D pieces, buckles, races, physics)
  scripts/
    bp.py                         the command line
    lib/                          the toolkit (pure Python + numpy + Pillow)
    data/known_tech.json          what retail uses (measured registry)
    tests/                        selftest
```

## Credits and data sources

- [wago.tools](https://wago.tools) - retail DB2 tables and files by FileDataID.
- [wowdev/wow-listfile](https://github.com/wowdev/wow-listfile) - the community listfile.
- [wowdev.wiki](https://wowdev.wiki) - M2, BLP, DB2 and PHYS format documentation.
- WoW Model Viewer and wow.export sources - geoset and item-rule cross-checks.
- Rules were measured against the WotLK 3.3.5a (12340) client and retail 12.1.0.69933.

## Legal

This is a fan tool, not affiliated with or endorsed by Blizzard Entertainment. It ships no Blizzard
assets: it downloads data from public community sources at run time, for your own use. World of
Warcraft and its assets are trademarks and copyright of Blizzard Entertainment.
