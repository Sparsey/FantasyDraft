# Following the ESPN draft room automatically

A Tampermonkey userscript passes the draft page's live feed to the `room`
command on your computer. `room` writes every pick, and every autodraft
on/off, into `picks.txt` as it happens. `watch` (in a second terminal)
re-shows recommendations after each pick. You don't type anything.

Works for ESPN mock drafts and real league drafts alike.

## One-time setup

1. Install the **Tampermonkey** browser extension.
   - **Chrome/Edge:** go to `chrome://extensions` → Tampermonkey → Details and
     turn on **Allow user scripts**. If that option isn't there, turn on
     **Developer mode** on the extensions page instead.
2. Tampermonkey dashboard → **+** (new script). Replace the template with all
   of [`espn_draft_capture.user.js`](espn_draft_capture.user.js), and save. Check
   it shows **version 0.5** or later (to update, replace the whole script and save).
3. The script only runs on espn.com pages with "draft" in the address, and
   only sends data to your own computer (`127.0.0.1`).

## Each draft

1. Create the league file (for a mock: your draft position and team count):
   ```bash
   python -m fantasydraft new-league mock.json --teams 10 --my-pick 8 --players "<sheet link>" --force
   ```
   For your real league, use your `league.json` instead.
2. Start the dashboard **before** opening the draft room. This is the only
   thing you need running:
   ```bash
   python -m fantasydraft ui --league mock.json --picks mock3.txt
   ```
   It opens in its own window (Chrome/Edge app mode; `--tab` for a normal
   tab). Snap it to one half of the screen and the draft room to the other.
3. Open the draft room and reload it once. The dot at the top right turns
   green ("live from draft room"), and picks appear by themselves.

Use a new picks file (`mock3.txt`, …) for each mock. Restarting `ui` with
the same file resumes.

### The dashboard

- **Top bar:** pick number, who's on the clock with the countdown, how many
  picks until yours, and feed status. The search box records a pick by hand
  (ESPN rank or name, then Enter) if the feed isn't running.
- **Pick strip:** each pick up to yours, with who that team will likely take.
- **Recommendations:** a summary line (target at your pick, ESPN's pick if
  different, any GAMBLE, who is likely best left), then the board: available
  players by your combined projections, ESPN vs Mine, chance each is there at
  your pick, with lines marking where it's 50%+ and 90%+ from there down. The
  target row is highlighted; tags mark ESPN's pick, GAMBLE and FALLING
  players; arrows show chances that moved since the last pick. **Sleepers**
  lists players your sources rate well above ESPN. `+` (only without a live
  feed) records a player as the next pick.
- **Teams:** rolling score (value gained vs. pick slot), starting-lineup
  rank, the model used for each team (dropdown, saved to the league file;
  hover a team for its details) and the autodraft switch.
- **Troubleshooting log**, collapsed.

ESPN's own room already shows recent picks, your roster and every team's
roster, so the dashboard doesn't repeat them.

The terminal pair `room` + `watch` still works if you prefer it.

### What it handles

- **ESPN team numbers ≠ draft positions.** The mapping is worked out from who
  is on the clock at each pick.
- **Autodraft and timeouts.** ESPN switches a team to autodraft when it
  times out. Each pick also shows whether a person or autodraft made it, so
  `room` marks teams `@auto`/`@back` automatically. Their saved strategy is
  kept for when they come back.
- **Players missing from your sheet** are recorded as "Unknown player <id>",
  so later picks still go to the right teams. Pull more players into the
  sheet (Settings → limit) if this happens a lot.
- **Joining late / reloading.** Restarting `room` with the same picks file
  resumes. If you join a draft already under way with no picks file, `room`
  reads the draft page's pick list to catch up. If that list isn't fully
  visible, it warns you and holds new picks until it can (try opening the
  **Pick History** tab in the draft room).
- **Wrong draft position.** If ESPN's data says you draft at a different
  position than your league file, `room` tells you.

## Troubleshooting / recording raw data

- Nothing in terminal 1: work through the checks in the earlier setup
  (open `http://127.0.0.1:8765/` in the browser; check the bottom-left badge
  on the draft page).
- `python -m fantasydraft capture --out x.jsonl` records raw traffic only,
  and `capture --out x.jsonl --summary s.txt` makes a redacted summary you
  can share. **Never share the `.jsonl` files**; they can contain login
  tokens. They're git-ignored.
