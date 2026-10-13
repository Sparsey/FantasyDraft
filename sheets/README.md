# Google Sheets setup

Your Google Sheet is the master copy of the player list. You can edit it any
time. It only pulls new ESPN data when you click **Draft Tools → Update
players from ESPN**, and the draft program reads it straight from the share link.

## One-time setup

1. Open the sheet → **Extensions → Apps Script**.
2. Delete the placeholder code, paste in all of [`espn_update.gs`](espn_update.gs), and click **Save**.
3. Reload the sheet. A **Draft Tools** menu appears (it can take a few seconds).
4. **Draft Tools → Settings…** and enter:
   - **League id**: the number after `leagueId=` in your ESPN league URL.
   - **Season**: the year the season *ends* (2026-27 season → `2027`).
   - **espn_s2 / SWID**: only needed for a **private** league. Get them on
     espn.com while logged in: DevTools (F12) → Application → Cookies →
     `https://www.espn.com`. Include the `{ }` in SWID.

   The first run asks you to authorise the script to reach ESPN and edit the
   sheet. These settings are stored in your Apps Script user properties, not
   in the sheet, so your ESPN login stays private even though the sheet is public.
5. **Draft Tools → Update players from ESPN.**

## Before your league is activated

Until the league is renewed for the new season, ESPN has no league to read,
so an update falls back to **ESPN's default league**. That still gives you
ranks, ADP, positions and projections, but the projections use ESPN's
default scoring rather than yours. A pop-up tells you when this happens,
and the note on the `id` header cell records which scoring the current
numbers use.

Once the league is activated (and your cookies are set, since it's
private), click **Update** again. Rows are matched by id, so the projections
are simply overwritten with your league's scoring, and your own columns
stay as they are.

You can enter the league id, season and cookies now. Use the same league id
as last season, because ESPN keeps it when a league is renewed.

## The players tab

The script uses the tab named `players`, or the first tab if there isn't one.
Row 1 holds the headers, in any order, and the headers are what it goes by.

| column | who fills it | meaning |
|---|---|---|
| `id` | ESPN | ESPN player id; how rows are matched on later updates |
| `name`, `positions`, `nba_team` | ESPN | `positions` like `PG/SG` |
| `proj_points` | ESPN | projected season fantasy points with **your league's scoring** |
| `espn_rank` | ESPN | ESPN draft rank (what autodraft roughly follows) |
| `adp` | ESPN | average draft position |
| `my_proj` | you | overrides `proj_points` when filled in |
| `ignore` | you | `x` / `yes` leaves the player out entirely (injured, won't draft…) |
| anything else | you | notes, tiers, formulas; the program ignores them |

An update rewrites only the ESPN columns. It matches rows by `id` and adds
new players at the bottom. It never deletes or reorders rows, and never
touches your own columns, so formulas and notes survive. You can sort and
filter the sheet freely.

## Pointing the program at the sheet

Keep the sheet shared as **Anyone with the link → Viewer**, then either pass
the link each time:

```bash
python -m fantasydraft recommend --players "https://docs.google.com/spreadsheets/d/<id>/edit?gid=0#gid=0"
```

or set it once in `league.json`:

```json
{ "players": "https://docs.google.com/spreadsheets/d/<id>/edit?gid=0#gid=0", ... }
```

The `gid` in the link picks the tab. The program downloads a fresh copy every
time it loads, so your edits show up on the next `recommend`. In `watch`, they
show up on the next pick (or save `league.json` to force a refresh).

Players with no projection, and blank rows, are skipped.
