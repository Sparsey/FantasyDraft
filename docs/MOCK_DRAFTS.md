# Practising with ESPN mock drafts

ESPN mock drafts are useful for two things:

1. **Learning how ESPN's bots and autodraft pick.** Check whether they follow
   ESPN rank, ADP or projections, and how much randomness there is.
2. **Rehearsing the draft-day loop**: the `watch` screen updating while picks
   come in.

Mock drafts aren't real leagues, so `live` can't follow them. Track them
with the interactive `draft` command, or copy the results off the page
afterwards and run `import-picks`.

## Setup

Create a league file for the mock. Run this once per mock, changing
`--my-pick` to your draft position:

```bash
python -m fantasydraft new-league mock.json --teams 10 --my-pick 4 --players "<your Google Sheet link>"
```

The other teams are saved with the `adp` strategy (`--strategy` changes it).
Add `--names "A,B,C,..."` to use real team names, and `--force` to overwrite
an existing `mock.json`. For a different roster size, edit `roster_slots` in
the file.

## 1. Track the mock live

```bash
python -m fantasydraft draft --league mock.json --picks picks.txt
```

Type each pick as it happens. The quickest is the player's **ESPN rank**
(e.g. `17`), but a name or the start of one (`jok` for Jokić, if unique)
works too. The confirmation shows the player's name; type `undo` if it's
wrong. Ranks come from your sheet, so update the sheet right before the
draft to match what the draft room shows. Recommendations refresh after every entry.

When someone switches autodraft on or off:

```
auto 3        # team at draft position 3 went on autodraft (a team name works too)
back 3        # they're drafting themselves again
```

While a team is marked auto, the simulation has it take the best player by
ESPN rank. `back` returns it to its saved strategy, which is never
overwritten. Picks made while on auto are also left out of that team's
`analyze` stats, so they don't distort how you read the manager.

Other commands: `undo` (removes the last pick or switch), `teams` (who's on
auto), Enter (refresh), `help`, `quit`. Everything is saved to `picks.txt`
as you go, so you can quit and resume.

Mock pick clocks are short. If recommendations lag, lower `--sims`
(default 60; try 30).

**Help automate this:** while you do a mock, you can also record the
draft page's live feed. See [browser/README.md](../browser/README.md).

## 2. Analyse the finished mock

1. On the draft results/recap page, select the list of picks in pick order,
   copy it, and paste it into a text file (say `mock1.txt`). Extra text like
   teams, positions and pick numbers is fine; player names are found in the
   text, and `F. Last` abbreviations work when unambiguous.
2. Import and analyse (skip the import if you tracked the mock with `draft`):

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
