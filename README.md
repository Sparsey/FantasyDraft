# FantasyDraft

Draft assistant for an ESPN fantasy basketball (points) league. It tracks a
snake draft, models how each opponent drafts, and uses Monte Carlo
simulation of the rest of the draft to recommend picks.

Pure Python 3.10+ standard library; `pytest` is only needed for tests.
See [docs/PLAN.md](docs/PLAN.md) for the roadmap and design notes.

## Quick start (sample data)

```bash
# rehearse: fake draft writes picks.txt while `watch` updates recommendations
python -m fantasydraft mock  --league examples/league.json --delay 3 &
python -m fantasydraft watch --league examples/league.json
```

`data/sample_players.csv` is synthetic (`examples/make_sample_players.py`).

## Real draft

1. **Players**: either
   - **Google Sheet (recommended):** follow [sheets/README.md](sheets/README.md).
     The sheet gets a *Draft Tools → Update players from ESPN* menu that runs
     only when you click it, and you can add your own `my_proj` overrides,
     `ignore` flags and notes. Then put the sheet link in `league.json` as
     `"players"` (or pass `--players <link>`).
   - **Local CSV:** download ESPN ranks, ADP and projected points (scored
     with your league's settings):
     ```powershell
     # private leagues: cookies from espn.com (PowerShell; bash: export ESPN_S2=...)
     $env:ESPN_S2 = "..."; $env:ESPN_SWID = "{...}"
     python -m fantasydraft fetch-players --league-id 12345 --season 2027
     ```
2. **League** — `python -m fantasydraft fetch-league --league-id 12345 --season 2027`
   writes `league.json` with draft order and roster slots. Then set
   `my_team` and each team's profile:

   | field | meaning |
   |---|---|
   | `strategy` | `unknown` (default: modelled by whichever of adp/rank/points best fits their picks so far), `auto` (starts the draft on autodraft: the dashboard's autodraft switch begins on), `adp` (drafts around ADP), `rank` (follows ESPN's board), `points` (drafts by projections), `balanced` (knowledgeable, builds a well-rounded team: projections scaled by roster need, low noise), `feel` (picks fairly evenly among ESPN's top `spread` players, default 8). Set it yourself when you know the person |
   | `spread` | for `feel` teams: how many of ESPN's top-ranked players they choose among (default 8) |
   | `noise` | starting guess for how unpredictable they are (0.15 ≈ ±15% of draft slot). Updated from their own picks during the draft unless `learn_noise` is `false` |
   | `favorites` | `{"Player Name": 10}` → takes that player ~10 spots early |
   | `lineups` | `daily` (ESPN's default: bench players' games still count ~85% of the time, so the bench is worth 0.86 of a starter, with small discounts for stacking a position or having only one player at it) or `weekly` (bench 0.4). `custom` lets you set `bench_weight`, `balance_penalty` and `cover_rate` yourself |
   | `players` | players CSV path or Google Sheets link (league-wide) |
   | `position_targets` | roster balance, e.g. `{"PG": 3, "SG": 3, "SF": 2, "PF": 2, "C": 3}`. Default: an even split rounded up (3 each for 13 players). Multi-position players count wherever there's room |
   | `learn_noise` | learn each opponent's noise (and show their best-fitting strategy) from their picks; default `true` |
   | `position_bias` | opponents (not on autodraft) are less likely to take a player at a position they've already filled to the targets: that player's draft slot is pushed back by this fraction (default `0.35`; `0` turns it off) |
   | `sources` | extra projection sources `{name: link}` or `{name: {"url": ..., "weight": 0.5, "games": "projected"}}`, averaged with ESPN into your value (see [sheets/README.md](sheets/README.md#extra-projection-sources)) |
   | `scoring` | fantasy points per stat, e.g. `{"PTS": 1, "REB": 1, "AST": 2, "STL": 4, "BLK": 4, "TO": -2, ...}`; default ESPN standard points; `fetch-league` fills it |
   | `managers` | per-person profiles, attached to teams by owner or team name: `{"Person": {"match": ["Team name"], "strategy": "balanced", "noise": 0.08, "view": "consensus", "favorite_team": "BOS"}}`. `view` = whose projections they trust (`espn`, `consensus`, `mix`). `history --write` fills these from a past draft |
   | `balance_penalty` | how much a player beyond the targets is discounted (default `0.2` = worth 80%; `0` turns balancing off) |

3. **Draft day**: the most reliable option is following the draft room
   itself with `room` + `watch` (see [browser/README.md](browser/README.md)).
   Alternatively, use the league API, in two terminals:
   ```bash
   python -m fantasydraft live  --league-id 12345 --season 2027   # ESPN -> picks.txt
   python -m fantasydraft watch                                   # picks.txt -> recommendations
   ```
   `live` also marks teams as on/off autodraft from ESPN's per-pick flag
   (written as `@auto` / `@back` lines). If the ESPN feed breaks, switch to
   `python -m fantasydraft draft` and type picks there, or type them into `picks.txt` yourself (one name or
   id per line, unique name prefixes are fine) — `watch` doesn't care where
   they come from.

## Commands

| command | what it does |
|---|---|
| `ui` | **draft dashboard** in its own window: live draft-room feed + recommendations + teams, one process ([browser/README.md](browser/README.md)) |
| `draft` | interactive tracker: type picks and `auto`/`back` switches, recommendations after each |
| `room` | follow the ESPN draft room live via a browser userscript and write `picks.txt` automatically ([browser/README.md](browser/README.md)) |
| `study` | all your mocks in `mocks/` at once: where each pick sat on ESPN's rank, ADP and projections, by autodraft vs people, stage of the draft, and position need; which list each person follows. Quick; paste the output to tune the opponent models |
| `sources` | check extra projection sources; who ESPN under/overrates |
| `history` | how each league member drafted in a past season (`--write league.json` saves profiles) |
| `capture` | record raw draft-room traffic (troubleshooting) |
| `new-league` | create a league file (e.g. `mock.json` for an ESPN mock) |
| `recommend` | rank options for my next pick (one-shot) |
| `watch` | re-run `recommend` whenever `picks.txt` or `league.json` changes |
| `availability` | P(each top player survives to my next 3 picks) |
| `analyze` | opponents' tendencies so far (autodraft? reaching? by points?) |
| `import-picks` | build `picks.txt` from text copied off a draft results page (see [docs/MOCK_DRAFTS.md](docs/MOCK_DRAFTS.md)) |
| `mock` | simulate a draft into `picks.txt` (prompts you on your turns unless `--auto-me`) |
| `fetch-players`, `fetch-league`, `live` | ESPN integration |

### Reading `recommend`

- **gain now** — how much your best lineup improves immediately.
- **avail@my pick / avail@pick after** — chance opponents leave the player
  for your next pick / the one after (if you pass on them).
- **E[final team]** — average projected value of your final roster over
  simulations where you take this player at your next pick (falling back to
  your default pick if they're gone). This is the number to sort by: it
  accounts for scarcity, what will still be there later, and opponents'
  tendencies.

## How it works

- `lineup.py` — optimal starters via matroid greedy + bipartite matching
  across PG/SG/SF/PF/C/G/F/UTIL slots.
- `drafters.py` — opponent pick models (strategy + noise + favorites).
- `simulate.py` — plays out the rest of the draft; availability odds.
- `recommend.py` — evaluates candidates on common random seeds.
- `analyze.py` — opponents' pick statistics.
- `learn.py` — learns each opponent's noise and best-fitting strategy from their picks (likelihood of each pick under the pick model).
- `sheets/espn_update.gs` — Apps Script behind the sheet's update menu.
- `espn.py` — ESPN's unofficial `lm-api-reads.fantasy.espn.com` API
  (untested against a live league so far).

## Tests

```bash
pip install pytest && python -m pytest
```
