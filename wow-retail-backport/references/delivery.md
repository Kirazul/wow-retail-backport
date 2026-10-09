# Delivery: what ships, where it goes, how it coexists

## What `pack` writes (W/out/)

```
out/
  patch-<X>.MPQ              the client archive (every file the rows reach + 3 tables)
  dbc/ItemDisplayInfo.dbc    the same 3 tables, for the SERVER's dbc folder
  dbc/Item.dbc
  dbc/HelmetGeosetVisData.dbc
  sql/item_template.sql      the server's items
  INSTALL.txt                player + server-admin steps, written for this client
  extension/                 only with build --extension: extension.json + patch-<X>-ext.MPQ (Tier 2)
```

## Paths: private by construction

| What | Archive path |
|---|---|
| model textures (type 0) | `Item\ObjectComponents\<ns>\<fdid>.blp` |
| row textures (type 2, capes) | `Item\ObjectComponents\<Head|Shoulder|Weapon|Shield|Cape>\<ns>\<fdid>.blp` |
| weapons / shields / shoulders | `Item\ObjectComponents\<Weapon|Shield|Shoulder>\<ns>\<stem>.m2` (+ `00.skin`, `.anim`) |
| helms | `Item\ObjectComponents\Head\<ns>\<stem>_<Prefix><M|F>.m2` |
| body regions | `Item\TextureComponents\<Region>Texture\<ns>\<name>_<U|M|F>.blp` |
| icons | `Interface\Icons\<icon_prefix>_<fdid>.blp` |
| tables | `DBFilesClient\ItemDisplayInfo.dbc`, `Item.dbc`, `HelmetGeosetVisData.dbc` |
| Tier 2 pieces | `Item\ObjectComponents\Collections\<ns>\<stem>_<Prefix><M|F>...m2` |

`<stem>` is retail's own file name (community listfile), plus `__<8 hex>` when
retail-only slots or particle colours were baked for a colourway, plus
`__s<6 hex>` when a geoset filter was applied. `pack` refuses any art path
outside `\<ns>\` or the icon prefix ("PRIVATE" gate): nothing we ship can
replace a file of the client or of another patch.

## Archive name and priority

The client mounts `Data\patch-?.MPQ` (one character) sorted descending by
name - `patch-Z` beats `patch-Y` - above every base archive; root patches beat
locale patches (wow.exe 0x00405AB0, client.py). Our archive must outrank every
archive that ships one of the three tables, because the client reads the
WINNING copy of a table. `scan` picks the first free letter above the highest
mounted patch. If `patch-Z.MPQ` is already taken, no single letter outranks it:
set `bp.json` "patch_name", or merge the three tables into the top patch
yourself (they are that patch's tables plus our rows only if you rebuilt them
from the winning copies - re-run scan/tables first).

## The three tables: why they are safe to replace

They are the client's own winning copies (scan read them) with rows APPENDED -
no client row is changed or removed. If the server or another patch changes
those tables later, our copy goes stale: re-run `scan`, `tables`, `audit`,
`pack` and redistribute. INSTALL.txt says so.

## Ids

| Id | Default | Config |
|---|---|---|
| display | above the client's max ItemDisplayInfo id, rounded up to 1000 | `display_id_start` |
| item | 900000 upward (pick a range your server's item_template does not use) | `item_id_start` |
| helm vis | above the client's max HelmetGeosetVisData id | `helmvis_id_start` |
Ids are stable across runs (W/ids.json): rebuilding never renumbers.

## Server (AzerothCore / TrinityCore 3.3.5)

1. Copy `out/dbc/*.dbc` into the server's `dbc` folder (back it up first).
   The server checks `item_template.displayid` against ItemDisplayInfo.dbc and
   reads Item.dbc.
2. Run `out/sql/item_template.sql` on the world database (it deletes and
   inserts only our entries). Items are cosmetic copies: any class, any race,
   item level 1, no stats, quality and name from retail.
3. Restart the worldserver. `.additem <id>` to test.

## Player

Copy `out/patch-<X>.MPQ` to `<WoW>\Data\`, delete `<WoW>\Cache`, start the
game. To remove: delete the archive and the Cache folder. Ship the archive and
the server tables together: an old archive with new tables (or the reverse)
shows wrong or invisible items.

## Size

One archive stays below 2 GB (the 3.3.5 MPQ reader is 32-bit). `pack` splits
by itself: art beyond 1.5 GB (uncompressed) goes to further archives named
with the LOWEST free patch letters scan found; the three tables stay only in
the top archive (`patch_name`). All archives of a project ship together.

## Git and files

Never commit retail files, DB2 CSVs, the cache, MPQs or the work folder. The
work folder lives outside any repository.
