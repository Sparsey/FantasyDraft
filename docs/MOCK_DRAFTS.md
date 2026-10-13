# Practising with ESPN mock drafts

ESPN mock drafts are useful for two things:

1. **Learning how ESPN's bots and autodraft pick.** Check whether they follow
   ESPN rank, ADP or projections, and how much randomness there is.
2. **Rehearsing the draft-day loop**: the `watch` screen updating while picks
   come in.

Mock drafts aren't real leagues, so `live` can't follow them. Instead, copy
the results off the page and run `import-picks`.

## Setup

Copy `examples/espn_mock_league.json` to `mock.json` and edit it to match the
mock you join:
- `players`: your Google Sheet link
- the number of teams (one entry per team, in first-round order)
- `my_team`: your draft position. Move the `"Me"` entry to that spot.
- `roster_slots`: if the mock uses a different roster size

## 1. Rehearse live (optional)

Keep `picks.txt` open in an editor and type each pick as it happens. A name,
or the first few unique letters of one, is enough per line. In another
terminal, run:

```bash
python -m fantasydraft watch --league mock.json --picks picks.txt
```

Each save refreshes the recommendations. Mock pick clocks are short, so
dropping `--sims` to 40 helps if the screen lags behind.

## 2. Analyse the finished mock

1. On the draft results/recap page, select the list of picks in pick order,
   copy it, and paste it into a text file (say `mock1.txt`). Extra text like
   teams, positions and pick numbers is fine; player names are found in the
   text, and `F. Last` abbreviations work when unambiguous.
2. Import and analyse:

   ```bash
   python -m fantasydraft import-picks mock1.txt --league mock.json --picks mock1_picks.txt
   python -m fantasydraft analyze --league mock.json --picks mock1_picks.txt
   ```

`import-picks` tells you if the count doesn't match teams × rounds. That
usually means a player missing from your sheet, which happens when the
update pulled fewer players than the mock drafted; raise `limit` in Settings.

**Copy the list view, not the grid board.** On the grid board, every other
round runs right to left, so the order would come out wrong.

## What to look for in `analyze`

- **took top ESPN rank ≈ 100%**: that team drafts strictly by ESPN rank.
  If the bots or autodrafted teams look like this, the `auto` strategy is
  right, and `espn_rank` in your sheet is the list they follow.
- **Lower, but "rank - pick" close to 0**: they follow ESPN rank loosely,
  which is the `rank` strategy with some `noise`.
- **"ADP - pick" consistently positive**: they reach for players early.
- **took top proj high**: they draft by projections (`points`).

A few mocks give you a sense of ESPN's bots, which will be the
autodrafters in your real draft.
