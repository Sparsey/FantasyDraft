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

## Draft-room rankings

ESPN's draft room loads its rankings from a separate request
(`kona_player_info_edit_draft_strategy`) with two lists: **STANDARD** (the
default draft-room ranks) and **ROTO**. The update fetches that request and
uses its ranks for `espn_rank`. The pop-up and header note say where the
ranks came from.

Before your league is activated, that request may have no ranks. In that
case, put a mock draft's league id in Settings → "ranks league". It's in
the mock draft room's URL (`leagueId=...`), and its rankings get used
instead. `python -m fantasydraft capture-ranks <capture.jsonl> --league
league.json` reads the same rankings out of a draft-room capture and
compares them with your sheet.

## Extra projection sources

Other sites' projections, averaged with ESPN's, become *your* value for each
player. Opponents are still modelled on ESPN's numbers, because that's what
the draft room shows them. The gap between the two is your edge.

1. Add a tab per source (e.g. `src_hashtag`). Either paste the site's
   projections table into A1, or use
   `=IMPORTHTML("https://hashtagbasketball.com/fantasy-basketball-projections", "table", 1)`
   (try 2 or 3 if 1 is the wrong table).
   If a page is too big for `IMPORTHTML` (e.g. Basketball-Reference), skip
   the tab and put the page's address in the league file directly; the
   program reads its stats table itself.
2. Copy that tab's link (it has `#gid=...` for the tab) into your league file:
   ```json
   "sources": {"hashtag": "https://docs.google.com/spreadsheets/d/<id>/edit#gid=<tab gid>"}
   ```
3. Check it: `python -m fantasydraft sources --league league.json`. It shows
   how many players matched, any scoring stats the source lacks, how the
   source compares with ESPN overall ("median vs ESPN" should be near 1.00
   when the scoring matches), and the players ESPN under- and overrates.

ESPN counts double by default (`"espn_weight": 2` in the league file), so
the other sources adjust ESPN's numbers rather than outvote them; each other
source counts once. A source can instead be given a weight, and for **last season's actual stats** you should use
per-game numbers × the games the projection sources expect, so players who
missed time last season aren't punished for it:

```json
"lastseason": {"url": "https://www.basketball-reference.com/leagues/NBA_2026_per_game.html",
               "weight": 0.5, "games": "projected"}
```

Columns are found by header: player name, GP, PTS, REB/TREB, AST, STL, BLK,
TO, 3PM (even when Sheets turns it into "3:00 PM"), and FG%/FT% as
`0.573(10.5/18.3)` or separate FGM/FGA/FTM/FTA. Per-game stats are
multiplied by GP. Scoring defaults to ESPN's standard points scoring. Set
`"scoring"` in the league file (`fetch-league` fills it from your league)
if yours differs. A `my_proj` value in the players tab still overrides
everything.

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

The `gid` in the link picks the tab. One-off commands (`recommend`,
`analyze`, …) download a fresh copy each time. `watch`, `draft`, `room` and
`ui` download it once when they start, which keeps refreshes fast during a
draft, so restart them to pick up sheet edits made mid-draft.

Players with no projection, and blank rows, are skipped.
