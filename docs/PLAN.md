# Plan & status

## Where things stand

| Area | Status |
|---|---|
| Player data | ✅ Google Sheet with an on-demand *Update players from ESPN* menu: ESPN projections (your league's scoring once activated), the draft room's own rankings (STANDARD/ROTO), ADP |
| Extra projection sources | ✅ FantasyPros, Hashtag (free top 30), last season (Basketball-Reference, half weight, per game × projected games); consensus = your value, ESPN = the room's view |
| Live draft feed | ✅ Tampermonkey userscript → `ui`/`room`: picks, autodraft on/off (incl. timeouts), team mapping, resync after rejoining |
| Dashboard (`ui`) | ✅ Half-screen window: pick strip with likely picks, recommendations with "take X over Y" callout, vs-ESPN + source agreement, ESPN-underrates tab, lineup with position targets, teams with strategies, autodraft switches |
| Opponent models | ✅ adp / rank / points / balanced / feel / auto / unknown; whose numbers they trust (ESPN / consensus / mix); favourite NBA team; position needs; noise and strategy learned from their picks |
| People | ✅ `managers` profiles matched to teams by owner/team name; `history` fits each person from a past draft |
| Evaluation | ✅ `benchmark` (auto vs points vs model on simulated drafts), `sources` report |

## What we learned

- ESPN's STANDARD rank is exactly ESPN's projection order, so autodrafters and
  board-followers draft by ESPN's projected points. Edge over the room comes
  from **knowing more than ESPN's numbers** (the consensus) and **knowing the
  people**, not from cleverer simulation.
- In a points league with flexible lineups, "best projected player" is right
  most of the time; position drop-offs (VONA) matter occasionally. The
  dashboard calls those moments out.
- The dashboard's **SAFE** pick is ESPN's top projection available (what the
  room and autodraft would take). Every other option is compared with it on
  final roster value by *your* consensus numbers, so a "+" there is your
  sources' edge over ESPN's board. **GAMBLE** is the best alternative that
  ends better in at least 40% of simulated drafts.
- Benchmarks score rosters with the consensus itself, so they show what the
  edge is worth *if* the consensus is right, not whether it is.

## To do before the draft

1. **League activation:** run `fetch-league` (draft order, roster slots,
   owners, *scoring*), click *Update* in the sheet, rerun `sources` and check
   "median vs ESPN" ≈ 1.00.
2. **History:** `history --league-id 358709800 --season 2026 --me "Aaron Solomon" --write league.json`
   to seed everyone's profile from last year's draft.
3. **Dress rehearsal:** one full ESPN mock with `ui` from pick 1, with a page
   reload and an autodraft toggle.
4. Hand-set `my_proj` for players you have strong views on (injury-prone
   stars, rookies).

## Ideas, lower priority

- ESPN position limits (e.g. max 4 C), counted by each player's primary
  position (needs a primary-position column from ESPN).
- Late-round upside from source disagreement.
- Backtest the consensus vs ESPN alone on last season's actual results.
- In-season tools (streaming for games played), reusing the sheet and sources.
