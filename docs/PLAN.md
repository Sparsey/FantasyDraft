# Plan & status

Your original overview, with what is built (✅), partly built (🟡), or still open (⬜),
plus notes on the open questions.

## Pre-draft

- 🟡 **Load player draft data into a CSV (sheets-friendly)**
  - `fetch-players` pulls ESPN's draft ranks, ADP and **projected fantasy points
    under your league's own scoring** (`appliedTotal` on the league endpoint)
    into `data/players.csv`. It is written against ESPN's undocumented API and
    has **not yet been run against a live league** (the dev sandbox can't
    reach ESPN), so run it once early and check the output. Use `--dump` to
    save the raw JSON if something looks off.
  - The CSV is plain columns (`id,name,positions,nba_team,proj_points,espn_rank,adp`),
    so you can round-trip it through Google Sheets, override projections, or
    merge in other rankings.
  - Other rankings: in a points league, projected points under *your* scoring
    is what matters; other sites' ranks are mostly useful as a better ADP
    signal for predicting opponents.
- ✅ **Give players/teams a drafter type** — in `league.json`, each team gets a
  `strategy` (`auto`, `adp`, `rank`, `points`), a `noise` level, and
  `favorites` (players they'll reach for, in draft spots).
- ✅ **Test that live update + UI work** — `mock` writes a simulated draft into
  `picks.txt` pick by pick while `watch` re-renders recommendations, so you
  can rehearse the whole loop without ESPN.

## Live draft

- 🟡 **Live update teams, snake order, who's on autodraft** — `live` polls
  ESPN's `mDraftDetail` view and mirrors picks into `picks.txt`; it reports
  which teams had picks flagged `autoDraftTypeId != 0`. Snake order is
  computed from `league.json` (`fetch-league` fills team order and roster
  slots from ESPN). Untested against a live draft; manual entry into
  `picks.txt` is the fallback.
- ✅ **Monte Carlo odds a player is available at my next pick** —
  `availability`, and the `avail@my pick` / `avail@pick after` columns.
  - ⬜ *Which ranking does ESPN autodraft use?* As far as I know: the team's
    queue first, then ESPN's default draft ranking (`draftRanksByRankType` —
    STANDARD, or a points-specific list if your league exposes one), with
    some roster-position limits. Not verified. **Test it**: create a mock
    draft lobby on ESPN with autodraft bots, then run `analyze` on the result
    — an autodrafter shows ~100% in "took top ESPN rank". If it follows a
    different list, fetch that list with `--rank-type` and use it as `espn_rank`.
- ✅ **"Would simple odds × points work?"** — It's a reasonable first
  approximation, but it ignores *opportunity cost* (what you'd take instead)
  and *positional scarcity*. What's implemented instead: for each candidate,
  simulate the **entire rest of the draft** many times (opponents by their
  profiles, your later picks by a greedy roster-fit policy) and compare your
  **expected final roster value**. All candidates share the same random seeds
  so differences come from the pick, not noise. This subsumes odds × points and
  relative/positional value.
- ✅ **Value to my team given positions** — roster value = optimal starting
  lineup (exact slot matching incl. G/F/UTIL) + `bench_weight` × bench. The
  "gain now" column is the immediate lineup improvement; "E[final team]" is
  the forward-looking version.
- 🟡 **Analyse other teams' strategies** — `analyze` reports, per team, how
  often they took the top ESPN-ranked or top-projected player, and how far
  they reach versus ADP and ESPN rank, with a suggested strategy. Not yet
  fed back automatically into `league.json` — update it by hand mid-draft
  (`watch` picks up the change).
  - ⬜ Save a summary after each draft to build per-manager history for next year.

## Ideas / next steps

- Validate the ESPN client against your real league (league id, season, cookies).
- Calibrate `noise` per strategy from past drafts (ESPN keeps prior seasons'
  draft results; the same `mDraftDetail` view works with an older `--season`).
- Better "my future picks" policy in simulations (currently greedy on
  immediate lineup gain; could use lookahead or a value-over-replacement rule).
- Games-played / schedule awareness: in H2H points with daily lineups, bench
  value depends on how often starters sit; `bench_weight` is a crude stand-in.
- Optional richer UI (e.g. a small web page or Google Sheets sync) on top of
  the same engine.
