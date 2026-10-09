# Bodies and races: fitting ANY 3.3.5a client

The kit never assumes which races a client has or what its bodies look like.
`scan` reads them from the client; `measure` measures how they differ from
retail's; `build` fits every item to them. This works the same on a stock
client, an HD-patched client and a custom realm client with extra races.

## 1. What `scan` learns (client/scan.json)

| Fact | How it is found |
|---|---|
| mounted archives, highest first | wow.exe's rule (client.py): `patch-?.MPQ` in Data and `patch-<loc>-?.MPQ` in Data\<loc> sorted DESCENDING by full path (so every root patch outranks every locale patch), then patch.MPQ / patch-<loc>.MPQ, then the base table order. Anything else (`*.disabled`, `backup-*.mpq`, names not in the table) is not mounted. `wildcard_patches: true` for a realm exe that mounts `patch-*.MPQ` (long names) |
| the winning copy of each table | the first mounted archive that holds it |
| races | every ChrRaces row: id, ClientPrefix (helm suffix) and ClientFileString (body folder) - their columns are FOUND (row 1 holds "Hu"/"Human", row 2 "Or"/"Orc") so a custom ChrRaces layout works |
| playable | ChrRaces.Flags (column 1) bit 0x1 = NOT playable (measured: 11 playable races clear, every NPC race sets it) |
| bodies | `Character\<File>\<Male|Female>\<File><Male|Female>.m2` + 00.skin if present: vertices, bones, geosets, helm point (attachment 11) |
| retail match | same id AND same prefix as retail ChrRaces -> that retail race; else the retail race with the same prefix; else `bp.json` "race_map" {"client id": retail id}; else none |
| free ids | max id of ItemDisplayInfo, Item, HelmetGeosetVisData |
| output archive | the first free single-letter `patch-?.MPQ` above every mounted one (our tables must win) |

## 2. A race with no retail match (custom clients)

`scan` prints `NO retail match`. Decide with the user which retail race's
items that body should wear (the one it was ported from, usually the same
skeleton), then set it and scan again:
```
"race_map": {"22": 22, "24": 26}
```
(keys are CLIENT race ids as strings, values RETAIL race ids). Retail race ids:
read retail ChrRaces (`find` shows sets; the table is in `W/db2/<build>/ChrRaces.csv`).
With no mapping, that race's helms are invisible stand-ins (never a checker
box) and its sheets show no helm - tell the user.

## 3. Which file a body wears (helms)

For client race R (retail match r) and sex s:
1. retail's own cut of the helm for (r, s) - ComponentModelFileData RaceID + GenderIndex;
2. else retail's fallback chain: ChrRaces Male/FemaleModelFallbackRaceID/Sex of r, followed until a cut exists;
3. else a race-agnostic model of the display (no CMFD row);
4. else an INVISIBLE stand-in under `<stem>_<Prefix><Sex>.m2` (valid model, every vertex at the origin, no effects).

Then: the slot's geoset filter (a combined set model keeps only its head
part); a body-space helm is made helm-point-local; finally the body's
measured helm shift is added.

## 4. Helm shift (measure -> client/helm_shift.json)

Retail puts the helm point (attachment 11) ON the head bone. 3.3.5 bodies put
it elsewhere - measured on a legacy client: Human male 6 cm forward and 18 cm
up of the head bone. A retail cut placed as-is floats ~20 cm high.

MEASURED (the rule): the client ships helm files retail also ships (same path
in the community listfile, same vertex count). Every such pair differs by one
constant per race and sex = the shift. The median over all pairs is used; the
90th-percentile spread is reported. Measured on a legacy client (2026-10-09):
107-176 pairs per playable body, spread 0.5-6.7 cm (Tauren widest), e.g.
Human male -5.8/0/-19.7 cm, Draenei male -7.5/0/-25.9 cm, Gnome female -1.8/0/-28.8 cm.
HD clients ship retail's helm files moved the same way, so the same measurement
holds for them.

ESTIMATED (fallback, only without pairs): keep the helm where it sits relative
to the head bone: shift = (retail helm point - retail head pivot) - (client helm
point - client head pivot). Marked "estimated": look at that body's sheet.
UNKNOWN (no pairs and no retail body): shift 0, flagged: look at the sheet;
for an NPC race it matters only if NPCs wear the items.

Gate P1 (audit) checks every built helm on every body: the helm must reach the
head (nearest vertex within retail's own distance + 8 cm, centroid within
12 cm of retail's, head-relative). A helm moved 35 cm is caught (prove-gates).

## 5. Body geoset map (measure -> client/geoset_map.json)

A row asks for `<group>01 + GeosetGroup`. Retail bodies have variants 3.3.5
bodies do not (a missing one shows NOTHING). For every retail variant, the
client variant of the closest shape is chosen: mean nearest-neighbour distance
both ways between the retail body's variant mesh and each client variant mesh
of the same race and sex, majority over the PLAYABLE bodies (NPC bodies are
often retail's own file and would out-vote them). Among playable bodies, only
retail-like ones vote (a shared variant within 1 cm) unless none is - then all
vote (a legacy client). Measured on a legacy client: boots 506->501, 507->501,
508..510->502; kneepads 905->902; pants 1105->1104; tabard 1203/1204->1202;
robe 1303->1302; belts 1801/1803/1804->1802; capes 1507..1519 -> 1503..1506.
Only GeosetGroup[0..2] reach the client (3.3.5 rows have three columns);
retail's [3..5] (chest 22xx/28xx, hands 23xx, feet 20xx) are Tier 2.

## 6. HD vs legacy vs custom bodies

Nothing is hard-coded per family: every number above is measured on the
client in hand. Legacy (stock) bodies: ~5,000 vertices, ~138 bones; HD
bodies: retail's own meshes, ~220 bones sharing retail's bone name hashes.
Both get correct helms (measured shift) and correct body geosets (measured
map). Retail 3D pieces (Tier 2) fit HD bodies by bone name; legacy bodies
need a surface transfer (references/client-extension.md).

## 7. NPC races

Built too (NPCs may wear items through CreatureDisplayInfoExtra), with the same
rules; their shifts are usually "estimated"/"UNKNOWN" (no helm pairs). The
sheet shows playable races only; check NPC races only when the user asks.
