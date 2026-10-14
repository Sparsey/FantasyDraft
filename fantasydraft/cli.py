"""Command line interface.

Typing picks yourself (ESPN mock drafts, or if the ESPN feed breaks):

    python -m fantasydraft draft --league mock.json --picks picks.txt

Following a real ESPN league automatically (two terminals):

    python -m fantasydraft live  --league-id 123 --season 2027   # ESPN -> picks.txt
    python -m fantasydraft watch --league league.json --picks picks.txt

`mock` writes a fake draft into picks.txt to rehearse the whole loop.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import shutil
import sys
import time
from pathlib import Path

from .analyze import analyze
from .drafters import CANDIDATE_POOL, choose_pick
from .io import load_log, load_picks, placeholder_player, load_state, player_by_rank, resolve_player, resolve_team, save_log, save_picks, save_players
from .models import DraftState
from .recommend import recommend
from .simulate import Simulator, availability


def _pct(p: float) -> str:
    """Percent for a simulated chance: <1% / >99% unless it never/always happened."""
    if p <= 0:
        return "0%"
    if p >= 1:
        return "100%"
    if p < 0.01:
        return "<1%"
    if p > 0.99:
        return ">99%"
    return f"{p:.0%}"


def _table(rows: list[list[str]], header: list[str]) -> str:
    widths = [max(len(str(x)) for x in col) for col in zip(header, *rows)]
    fmt = "  ".join(f"{{:<{w}}}" for w in widths)
    lines = [fmt.format(*header), fmt.format(*["-" * w for w in widths])]
    lines += [fmt.format(*map(str, r)) for r in rows]
    return "\n".join(lines)


_PLAYERS_CACHE: dict = {}


def _state(args, cached: bool = False) -> DraftState:
    """Load the draft state. With cached=True the players list (possibly a
    Google Sheet download) is fetched once and reused on later calls."""
    if not cached:
        return load_state(args.players, args.league, args.picks)
    key = (args.players, args.league)
    if key not in _PLAYERS_CACHE:
        _PLAYERS_CACHE[key] = load_state(args.players, args.league, None).players
    return load_state(args.players, args.league, args.picks, players=_PLAYERS_CACHE[key])


def _status(state: DraftState) -> str:
    league = state.league
    if state.is_complete:
        return "Draft complete."
    pick = state.current_pick
    rnd = pick // league.num_teams + 1
    team = league.teams[league.team_for_pick(pick)].name
    mine = state.my_next_picks()
    until = mine[0] - pick if mine else None
    lines = [f"Pick {pick + 1} (round {rnd}) — on the clock: {team}"]
    if until == 0:
        lines.append(">>> YOU ARE ON THE CLOCK <<<")
    elif until is not None:
        lines.append(f"Your next pick: #{mine[0] + 1} ({until} pick{'s' if until != 1 else ''} away)")
    if state.auto_teams:
        lines.append("On autodraft: " + ", ".join(league.teams[t].name for t in sorted(state.auto_teams)))
    roster = state.rosters()[league.my_slot]
    if roster:
        lines.append("Your positions: " + _positions_line(state))
        lines.append("Your roster: " + ", ".join(f"{p.name} ({'/'.join(sorted(p.positions))})" for p in roster))
    return "\n".join(lines)


def _positions_line(state: DraftState) -> str:
    """e.g. 'PG 1/3  SG 2/3  SF 1/3  PF 1/3  C 3/3' using flexible assignment."""
    from .lineup import target_fill

    league = state.league
    filled, extra = target_fill(state.rosters()[league.my_slot], league)
    parts = [f"{pos} {filled[pos]}/{n}" for pos, n in league.targets.items()]
    if extra:
        parts.append(f"(over targets: {', '.join(p.name for p in extra)})")
    return "  ".join(parts)


def _print_recommendations(state: DraftState, args) -> None:
    print(_status(state))
    if state.is_complete or not state.my_next_picks():
        return
    recs = recommend(state, candidates=args.candidates, sims=args.sims, seed=args.seed)
    if not recs:
        return
    best = recs[0].exp_final_value
    rows = [
        [
            r.player.name,
            "/".join(sorted(r.player.positions)),
            f"{r.player.proj_points:.0f}",
            f"{r.marginal_now:+.0f}",
            _pct(r.p_avail_now),
            _pct(r.p_avail_after),
            f"{r.exp_final_value:.0f}",
            f"{r.exp_final_value - best:+.0f}",
        ]
        for r in recs
    ]
    print()
    print(_table(rows, ["player", "pos", "proj", "gain now", "avail@my pick",
                        "avail@pick after", "E[final team]", "vs best"]))


def cmd_recommend(args) -> None:
    _print_recommendations(_state(args), args)


def cmd_availability(args) -> None:
    state = _state(args)
    print(_status(state))
    probs = availability(state, sims=args.sims, seed=args.seed)
    mine = state.my_next_picks()[:3]
    top = sorted(state.available(), key=lambda p: p.proj_points, reverse=True)[: args.top]
    rows = [
        [p.name, "/".join(sorted(p.positions)), f"{p.proj_points:.0f}",
         *(_pct(x) for x in probs[p.id][:3])]
        for p in top
    ]
    print()
    print(_table(rows, ["player", "pos", "proj", *(f"P(avail @#{m + 1})" for m in mine)]))


def cmd_import_picks(args) -> None:
    """Turn text copied from a draft results page into a picks file."""
    from .io import extract_picks, load_players, players_source

    players = load_players(args.players or players_source(args.league))
    from .io import read_text_file
    text = sys.stdin.read() if args.source == "-" else read_text_file(args.source)
    picks, warnings = extract_picks(text, players)
    for w in warnings:
        print(f"warning: {w}", file=sys.stderr)
    save_picks(picks, players, args.picks)
    print(f"wrote {len(picks)} picks to {args.picks}")
    try:
        league = load_state(args.players, args.league, None).league
        if len(picks) != league.total_picks:
            print(f"note: {args.league} expects {league.total_picks} picks "
                  f"({league.num_teams} teams x {league.rounds} rounds) — check for "
                  "players missing from your list, or a different league size.")
    except Exception:
        pass
    print("first picks: " + ", ".join(players[p].name for p in picks[:5]))


def cmd_analyze(args) -> None:
    from .learn import fit_teams

    state = _state(args)
    fits = fit_teams(state) if state.picks else {}
    rows = []
    for i, t in enumerate(analyze(state)):
        f = fits.get(i)
        best = max(f.fit, key=f.fit.get) if f else None
        rows.append([t.team, t.picks, t.auto_picks or "", f"{t.top_rank_rate:.0%}",
                     f"{t.reach_vs_adp:+.1f}",
                     f"{best} {f.fit[best]:.0%}" if f else "",
                     f"{f.prior_noise:g} -> {f.noise:.2f}" if f else ""])
    print(_table(rows, ["team", "own picks", "auto picks", "took top ESPN rank", "ADP - pick",
                        "best-fitting strategy", "noise (league file -> learned)"]))
    print("\n'ADP - pick' > 0 means the team takes players earlier than ADP (reaches).")
    print("Picks made while a team was marked @auto are left out of its stats.")
    print("Learned noise is what the simulations use (turn off with \"learn_noise\": false).")


DRAFT_HELP = """Record the next pick by typing the player's ESPN rank (e.g. 17),
or a name / start of a name, or id:<espn id>.
  auto <team>   team went on autodraft     (team = name, start of name, or draft position)
  back <team>   team is drafting themselves again (their saved strategy is used)
  undo          remove the last entry
  teams         show teams, strategies and who is on auto
  <Enter>       refresh recommendations
  help, quit"""


def _teams_table(state: DraftState) -> str:
    league = state.league
    rows = [
        [i + 1, t.name + (" (you)" if i == league.my_slot else ""), t.strategy,
         "ON AUTO" if i in state.auto_teams else ""]
        for i, t in enumerate(league.teams)
    ]
    return _table(rows, ["#", "team", "saved strategy", "now"])


def cmd_draft(args) -> None:
    """Interactive draft tracker: type picks and autodraft switches, get recommendations."""
    message = "Type 'help' for commands."
    show_recs = True
    while True:
        try:
            state = _state(args, cached=True)
            entries = load_log(args.picks, state.players, state.league)
        except Exception as e:
            print(f"error reading files: {e}")
            if input("fix the file and press Enter (or 'quit'): ").strip().lower() in ("q", "quit"):
                return
            continue
        print("\n" + "=" * 72)
        if show_recs:
            _print_recommendations(state, args)
        else:
            print(_status(state))
        show_recs = True
        print(f"\n{message}")
        try:
            line = input("> ").strip()
        except EOFError:
            return
        cmd, _, rest = line.partition(" ")
        cmd = cmd.lower()
        try:
            if not line:
                message = "Refreshed."
            elif cmd in ("q", "quit", "exit"):
                return
            elif cmd in ("h", "help", "?"):
                message, show_recs = DRAFT_HELP, False
            elif cmd == "teams":
                message, show_recs = _teams_table(state), False
            elif cmd == "undo":
                if not entries:
                    message = "Nothing to undo."
                else:
                    last = entries.pop()
                    save_log(entries, state.players, state.league, args.picks)
                    what = state.players[last[1]].name if last[0] == "pick" else \
                        f"{'auto' if last[2] else 'back'} {state.league.teams[last[1]].name}"
                    message = f"Undid: {what}"
            elif cmd in ("auto", "back", "manual") and rest:
                team = resolve_team(rest, state.league)
                on = cmd == "auto"
                entries.append(("auto", team, on))
                save_log(entries, state.players, state.league, args.picks)
                name = state.league.teams[team].name
                message = (f"{name} is on autodraft." if on else
                           f"{name} is back (strategy: {state.league.teams[team].strategy}).")
            else:
                if state.is_complete:
                    raise ValueError("the draft is already complete")
                if line.isdigit():
                    pid = player_by_rank(int(line), state.players)
                    if pid is None:
                        raise ValueError(f"no player with ESPN rank {line} in your players list")
                elif line.lower().startswith("id:"):
                    pid = line[3:].strip()
                    if pid not in state.players:
                        raise ValueError(f"no player with id {pid}")
                else:
                    pid = resolve_player(line, state.players)
                if pid in state.picks:
                    raise ValueError(f"{state.players[pid].name} is already drafted")
                team = state.league.teams[state.league.team_for_pick(state.current_pick)].name
                entries.append(("pick", pid))
                save_log(entries, state.players, state.league, args.picks)
                p = state.players[pid]
                rank = f" (ESPN rank {p.espn_rank:g})" if p.espn_rank != float("inf") else ""
                message = f"#{state.current_pick + 1} {team}: {p.name}{rank}   — wrong? type undo"
        except ValueError as e:
            message = f"!! {e}"


def cmd_new_league(args) -> None:
    """Write a league file for a draft (e.g. an ESPN mock) without editing JSON."""
    out = Path(args.out)
    if out.exists() and not args.force:
        sys.exit(f"{out} already exists (use --force to overwrite)")
    if not 1 <= args.my_pick <= args.teams:
        sys.exit(f"--my-pick must be between 1 and {args.teams}")
    names = [n.strip() for n in args.names.split(",")] if args.names else []
    if names and len(names) != args.teams:
        sys.exit(f"--names has {len(names)} names but --teams is {args.teams}")
    teams = []
    for i in range(args.teams):
        if i == args.my_pick - 1:
            teams.append({"name": names[i] if names else "Me", "strategy": "adp"})
        else:
            teams.append({"name": names[i] if names else f"Team {i + 1}",
                          "strategy": args.strategy, "noise": args.noise})
    cfg = {
        "players": args.players or "data/players.csv",
        "my_team": args.my_pick,
        "lineups": "daily",
        "roster_slots": {"PG": 1, "SG": 1, "SF": 1, "PF": 1, "C": 1, "G": 1, "F": 1, "UTIL": 3, "BE": 3},
        "teams": teams,
    }
    # Same players sheet, sources, scoring and settings as your real league,
    # but none of its teams or people (a mock has strangers in it).
    like = Path(args.like) if args.like else None
    if like and like.exists() and like.resolve() != out.resolve():
        base = json.loads(like.read_text(encoding="utf-8-sig"))
        keep = {k: v for k, v in base.items()
                if k not in ("teams", "my_team", "managers", "espn_team_ids")}
        if args.players:
            keep.pop("players", None)
        cfg = {**cfg, **keep}
        print(f"copied players, sources and settings from {like}: players = {cfg['players']}, "
              f"{len(cfg.get('sources') or {})} extra sources")
    elif not args.players:
        print(f"warning: {args.like} not found here, so players come from {cfg['players']} "
              "(not your sheet). Run this in your FantasyDraft folder, or pass --players <sheet link>.")
    out.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {out}: {args.teams} teams, you pick #{args.my_pick}, others '{args.strategy}'")


def cmd_watch(args) -> None:
    last = None
    while True:
        try:
            mtime = (Path(args.picks).stat().st_mtime if Path(args.picks).exists() else 0,
                     Path(args.league).stat().st_mtime)
            if mtime != last:
                last = mtime
                state = _state(args, cached=True)
                os.system("cls" if os.name == "nt" else "clear")
                _print_recommendations(state, args)
        except Exception as e:  # keep watching through half-written files
            print(f"error: {e}", file=sys.stderr)
            last = None
        time.sleep(args.interval)


def cmd_mock(args) -> None:
    """Write a simulated draft into the picks file (overwriting it), one pick at a time."""
    state = load_state(args.players, args.league, None)
    league = state.league
    rng = random.Random(args.seed)
    sim = Simulator(state)
    save_picks([], state.players, args.picks)
    for pick in range(min(args.until or league.total_picks, league.total_picks)):
        slot = league.team_for_pick(pick)
        team = league.teams[slot]
        taken = set(state.picks)
        if slot == league.my_slot and not args.auto_me:
            while len(state.picks) == pick:
                input(f"#{pick + 1:>3} you're up — add your pick to {args.picks}, then press Enter ")
                state.picks = load_picks(args.picks, state.players)
            print(f"#{pick + 1:>3} {team.name:<20} {state.players[state.picks[-1]].name}")
            continue
        if slot == league.my_slot:
            choice = sim.my_greedy_pick(state.rosters()[slot], taken)
        else:
            prof = sim.profile(slot)
            choice = choose_pick(prof, sim.top_untaken(prof.order_key, taken, CANDIDATE_POOL), rng, pick)
        state.picks.append(choice.id)
        save_picks(state.picks, state.players, args.picks)
        print(f"#{pick + 1:>3} {team.name:<20} {choice.name}")
        time.sleep(args.delay)


def cmd_fetch_players(args) -> None:
    import urllib.error

    from .espn import EspnClient, parse_player

    client = EspnClient(args.league_id, args.season, args.espn_s2, args.swid)
    defaults = args.defaults
    try:
        raw = client.players_raw(args.limit, args.rank_type, defaults)
    except urllib.error.HTTPError as e:
        if defaults:
            raise
        print(f"warning: league {args.league_id} not readable for {args.season} (HTTP {e.code}) — "
              "not activated yet, or missing/expired cookies. Falling back to ESPN default-league "
              "projections; re-run once the league works.", file=sys.stderr)
        defaults = True
        raw = client.players_raw(args.limit, args.rank_type, defaults)
    if args.dump:
        Path(args.dump).write_text(json.dumps(raw, indent=1))
    players = [p for r in raw if (p := parse_player(r, args.season, args.rank_type))]
    out = Path(args.out)
    if out.exists() and not args.force:
        from .io import PLAYER_COLUMNS, read_text_file
        header = {h.strip().lower() for h in read_text_file(out).split("\n", 1)[0].split(",")}
        extra = sorted(header - set(PLAYER_COLUMNS) - {""})
        if extra:
            sys.exit(f"{out} has your own columns ({', '.join(extra)}), which this would erase. "
                     "Use --out for a new file, or --force to overwrite.")
    save_players(players, args.out)
    source = "ESPN default scoring" if defaults else f"league {args.league_id} scoring"
    print(f"wrote {len(players)} players to {args.out} (projections: {source})")


def cmd_fetch_league(args) -> None:
    """Create/refresh league.json from ESPN, keeping any strategies you already set."""
    from .espn import EspnClient

    client = EspnClient(args.league_id, args.season, args.espn_s2, args.swid)
    data = client.league()
    if args.dump:
        Path(args.dump).write_text(json.dumps(data, indent=1))
    names = client.team_names(data)
    owners = client.team_owners(data)
    out = Path(args.out)
    existing = json.loads(out.read_text(encoding="utf-8-sig")) if out.exists() else {}
    old_teams = existing.get("teams", [])
    # Teams are matched by ESPN id (survives renames), else by name.
    by_id = {t["espn_id"]: t for t in old_teams if "espn_id" in t}
    by_name = {t["name"]: t for t in old_teams}
    # my_team as a draft position would point at someone else once ESPN
    # (re)sets the draft order, so remember it as a team.
    my = existing.get("my_team", 1)
    my_old = old_teams[my - 1] if isinstance(my, int) and 0 < my <= len(old_teams) else \
        next((t for t in old_teams if t["name"] == my), None)
    order = client.pick_order(data)
    teams, my_name = [], None
    for tid in order:
        prev = by_id.get(tid) or by_name.get(names[tid]) or {"strategy": "unknown", "noise": 0.2}
        entry = {**prev, "name": names[tid], "espn_id": tid, **({"owner": owners[tid]} if tid in owners else {})}
        if my_old is not None and prev is my_old:
            my_name = names[tid]
        teams.append(entry)
    scoring = client.scoring(data)
    # Everything you set by hand is kept; only what ESPN owns is refreshed.
    cfg = {
        "players": "data/players.csv", "lineups": "daily", "sources": {}, "managers": {},
        **existing,
        "my_team": my_name or my,
        "roster_slots": client.roster_slots(data),
        "scoring": scoring or existing.get("scoring"),
        "espn_team_ids": order,
        "teams": teams,
    }
    out.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    me = cfg["my_team"]
    print(f"wrote {out}. You are: {me if isinstance(me, str) else f'draft position {me}'}"
          " (check 'my_team' is right once ESPN sets the draft order)")


def cmd_live(args) -> None:
    """Poll ESPN's draft and mirror it into the picks file."""
    from .espn import EspnClient

    client = EspnClient(args.league_id, args.season, args.espn_s2, args.swid)
    seen = -1
    while True:
        try:
            data = client.league()
            picks = client.picks(data)
            if len(picks) != seen:
                seen = len(picks)
                state = _state(args)
                unknown = [p.player_id for p in picks if p.player_id not in state.players]
                if unknown:
                    print(f"warning: {len(unknown)} drafted players missing from the players list: {unknown[:5]}")
                # Mirror ESPN's per-pick autodraft flag as @auto/@back switches.
                slot_of = {tid: i for i, tid in enumerate(client.pick_order(data))}
                entries: list[tuple] = []
                on_auto: set[int] = set(state.league.start_auto)
                for p in picks:
                    # A player missing from your list is still a pick (kept as a
                    # placeholder) so later picks stay with the right teams.
                    state.players.setdefault(p.player_id, placeholder_player(p.player_id))
                    slot = slot_of.get(p.team_id)
                    if slot is not None and p.auto != (slot in on_auto):
                        entries.append(("auto", slot, p.auto))
                        (on_auto.add if p.auto else on_auto.discard)(slot)
                    entries.append(("pick", p.player_id))
                save_log(entries, state.players, state.league, args.picks)
                names = [state.league.teams[i].name for i in sorted(on_auto)]
                print(f"{len(picks)} picks synced" + (f"; on autodraft at their last pick: {', '.join(names)}" if names else ""))
        except Exception as e:
            print(f"error: {e}", file=sys.stderr)
        time.sleep(args.interval)


def cmd_replay(args) -> None:
    from .capture import replay_report

    state = load_state(args.players, args.league, None)
    text = replay_report(args.log, state.league, state.players,
                         session=args.session - 1 if args.session > 0 else -1)
    Path(args.out).write_text(text, encoding="utf-8")
    print(text[:6000])
    print(f"\n(full report in {args.out}; it has no cookies or tokens and is fine to share)")


def cmd_capture(args) -> None:
    from .capture import serve, summarize

    if args.summary:
        text = summarize(args.out)
        Path(args.summary).write_text(text, encoding="utf-8")
        print(text[:4000])
        print(f"\n(full summary written to {args.summary})")
    else:
        serve(args.out, args.port)


def cmd_ui(args) -> None:
    """Dashboard in the browser, fed live by the draft-room userscript (or manual entry)."""
    from .ui import Dashboard, serve

    dash = Dashboard(args.players, args.league, args.picks, args.out, sims=args.sims, candidates=args.candidates)
    serve(dash, args.port, open_browser=not args.no_browser, app_window=not args.tab)


def cmd_mock_draft(args) -> None:
    """One command for an ESPN mock: league file for this room + dashboard on a new picks file."""
    import argparse

    league = Path(args.league)
    cmd_new_league(argparse.Namespace(
        out=str(league), teams=args.teams, my_pick=args.pick, players=None, names=None,
        strategy="unknown", noise=0.2, like=args.like, force=True))
    Path("mocks").mkdir(exist_ok=True)
    picks = Path("mocks") / time.strftime("mock-%Y%m%d-%H%M.txt")
    shutil.copyfile(league, picks.with_name(picks.stem + ".league.json"))  # for `review` later
    print(f"picks go to {picks}")
    cmd_ui(argparse.Namespace(players=None, league=str(league), picks=str(picks), seed=0,
                              sims=args.sims, candidates=8, out=str(picks.with_suffix(".feed.jsonl")),
                              port=8765, no_browser=False, tab=False))


def cmd_review(args) -> None:
    """How your picks compare with ESPN's board, after a draft."""
    from .review import review

    picks = Path(args.picks) if args.picks else max(Path("mocks").glob("mock-*.txt"),
                                                     key=lambda p: p.stat().st_mtime, default=None)
    if picks is None or not picks.exists():
        sys.exit("no picks file given and none found in mocks/")
    league = args.league or next((str(p) for p in [picks.with_name(picks.stem + ".league.json")] if p.exists()),
                                 "mock.json")
    state = load_state(args.players, league, picks)
    print(f"{picks} with {league}\n")
    print(review(state, picks.with_name(picks.stem + ".decisions.jsonl")))


def _mock_files(args) -> tuple[Path, str]:
    picks = Path(args.picks) if args.picks else max(Path("mocks").glob("mock-*.txt"),
                                                     key=lambda p: p.stat().st_mtime, default=None)
    if picks is None or not picks.exists():
        sys.exit("no picks file given and none found in mocks/")
    saved = picks.with_name(picks.stem + ".league.json")
    return picks, args.league or (str(saved) if saved.exists() else "mock.json")


def cmd_model_check(args) -> None:
    """Does learning each team's habits improve the pick predictions?"""
    from .modelcheck import check

    picks, league = _mock_files(args)
    from .io import load_log

    state = load_state(args.players, league, picks)
    entries = load_log(picks, state.players, state.league)
    print(f"{picks} with {league}  (this takes a minute)\n")
    print(check(state, entries, sims=args.sims))


def cmd_study(args) -> None:
    """How the drafters in all your mocks actually pick."""
    from .study import find_drafts, study

    pairs = [(Path(p), Path(p).with_name(Path(p).stem + ".league.json")) for p in args.picks] \
        if args.picks else find_drafts(args.folder)
    if not pairs:
        sys.exit(f"no mocks found in {args.folder}/ (picks files with their .league.json next to them)")
    from .io import apply_league_sources, load_players, players_source

    drafts, loaded = [], {}
    for picks, league in pairs:
        league = str(league) if league.exists() else "mock.json"
        try:
            # Mocks usually share one players sheet: download it once.
            cfg = json.loads(Path(league).read_text(encoding="utf-8-sig"))
            key = (args.players or players_source(league), json.dumps(cfg.get("sources"), sort_keys=True),
                   cfg.get("espn_weight"))
            if key not in loaded:
                loaded[key] = load_players(key[0])
                apply_league_sources(loaded[key], league)
            st = load_state(None, league, picks, players=dict(loaded[key]))
        except Exception as e:  # one broken mock shouldn't stop the rest
            print(f"skipping {picks.name}: {e}")
            continue
        if len(st.picks) >= 2 * st.league.num_teams:
            drafts.append((picks.stem, st))
    print(study(drafts))


def cmd_room(args) -> None:
    """Follow an ESPN draft room through the browser userscript; write picks.txt live."""
    from .capture import serve
    from .room import RoomTracker

    state = load_state(args.players, args.league, None)
    tracker = RoomTracker(state.league, state.players, args.picks)
    print(f"Writing picks to {args.picks}. In another terminal run:\n"
          f"  python -m fantasydraft watch --league {args.league} --picks {args.picks}\n")

    def on_event(e: dict) -> None:
        if e.get("kind") == "ws-in":
            tracker.handle_message(str(e.get("data", "")))
        elif e.get("kind") == "dom-text":
            tracker.handle_page_text(str(e.get("data", "")))
        elif e.get("kind") == "hello":
            print(f"Draft page connected: {e.get('url', '')}")

    serve(args.out, args.port, on_event=on_event, quiet=True)


def cmd_sources(args) -> None:
    """Check extra projection sources and list who ESPN under/overrates."""
    from .io import apply_league_sources, load_players, players_source
    from .sources import underrated

    players = load_players(args.players or players_source(args.league))
    reports = apply_league_sources(players, args.league)
    if not reports:
        sys.exit(f'{args.league} has no "sources". Add e.g. "sources": {{"hashtag": "<link to the tab>"}}')
    for r in reports:
        print(f"\n{r.name}: {r.rows} rows, {r.matched} matched to your players"
              f" | median vs ESPN: {r.scale:.2f}x" + (" (rescaled to ESPN)" if r.calibrated else "")
              + (f" | weight {r.weight:g}" if r.weight != 1 else "")
              + (" | per game x projected games" if r.games == "projected" else ""))
        if r.estimated:
            print(f"  shooting attempts estimated from FG%/FT% for {r.estimated} players"
                  " (using each player's free-throw rate from sources that have attempts)")
        if r.missing_stats:
            print(f"  missing scoring stats: {', '.join(r.missing_stats)}")
        if r.unmatched:
            print(f"  not matched ({len(r.unmatched)}): {', '.join(r.unmatched[:12])}"
                  + (" ..." if len(r.unmatched) > 12 else ""))
    rows = underrated(players, top=args.top)

    def line(p, mine, espn):
        srcs = "  ".join(f"{n} {v:,.0f}" for n, v in p.sources)
        return [p.name, "/".join(sorted(p.positions)), mine, espn, f"{espn - mine:+d}",
                f"{p.proj_points:,.0f}", f"{p.proj_points - p.room_points:+,.0f}", f"±{p.spread:,.0f}", srcs]

    head = ["player", "pos", "my rank", "ESPN rank", "gap", "consensus", "vs ESPN", "spread", "by source"]
    print(f"\nESPN UNDERRATES (top {args.top} by consensus; you'd rank them higher than ESPN does):")
    print(_table([line(*r) for r in rows if r[2] - r[1] > 0][:args.show], head))
    print(f"\nESPN OVERRATES:")
    print(_table([line(*r) for r in reversed(rows) if r[1] - r[2] > 0][:args.show], head))


def cmd_capture_ranks(args) -> None:
    """Pull the draft room's rankings out of a capture and compare with projections."""
    import csv as _csv
    import dataclasses

    from .benchmark import rank_vs_projection
    from .espn import draft_ranks_from_capture
    from .io import load_players, players_source

    events = [json.loads(line) for line in Path(args.capture).read_text(encoding="utf-8-sig").splitlines() if line.strip()]
    ranks = draft_ranks_from_capture(events)
    if not ranks:
        sys.exit("no draft-room rankings found in this capture")
    kinds = sorted({k for r in ranks.values() for k in r if k != "name"})
    with open(args.out, "w", newline="", encoding="utf-8") as f:
        w = _csv.writer(f)
        w.writerow(["id", "name", *kinds])
        for pid, r in sorted(ranks.items(), key=lambda kv: kv[1].get(kinds[0], 1e9)):
            w.writerow([pid, r["name"], *(r.get(k, "") for k in kinds)])
    print(f"{len(ranks)} players with draft-room ranks ({', '.join(kinds)}) -> {args.out}")
    if args.league or args.players:
        players = load_players(args.players or players_source(args.league))
        matched = [p for p in players.values() if p.id in ranks]
        print(f"{len(matched)} of them are in your players list")
        for kind in kinds:
            with_room = {p.id: dataclasses.replace(p, espn_rank=float(ranks[p.id].get(kind, float("inf"))))
                         for p in matched}
            print(f"  {kind}: " + rank_vs_projection(with_room))
        top = sorted(matched, key=lambda p: ranks[p.id].get(kinds[0], 1e9))[:10]
        print(f"\nTop 10 by draft-room {kinds[0]} rank vs your sheet:")
        for p in top:
            print(f"  room #{ranks[p.id].get(kinds[0])}  sheet espn_rank {p.espn_rank:g}  proj {p.proj_points:.0f}  {p.name}")


def cmd_history(args) -> None:
    """How each league member drafted in a past season; optionally save as manager profiles."""
    from .history import fetch_history, merge_managers

    rows = fetch_history(args.league_id, args.season, args.espn_s2, args.swid, args.me or "")
    table = []
    for h in rows:
        fit = f"{h.best} {h.best_p:.0%}" if h.best else "-"
        table.append([h.owner or "?", h.team, h.picks, h.auto_picks or "", fit,
                      f"{h.noise:.2f}" if h.noise is not None else "-", f"{h.top_rank_rate:.0%}",
                      f"{h.reach_vs_adp:+.1f}", f"{h.favorite_team} x{h.favorite_count}" if h.favorite_team else ""])
    print(f"Draft {args.season} — league {args.league_id}")
    print(_table(table, ["owner", "team", "own picks", "auto picks", "best fit", "noise",
                         "took top ESPN rank", "ADP - pick", "favourite NBA team"]))
    print("\nnoise: 0.05 = very predictable, 0.3 = loose. 'ADP - pick' > 0 = reaches early.")
    if args.write:
        out = Path(args.write)
        cfg = json.loads(out.read_text(encoding="utf-8-sig"))
        cfg["managers"] = merge_managers(cfg.get("managers", {}), rows)
        out.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
        print(f"\nSaved profiles to {out} \"managers\" (anything you'd already set was kept).")


def cmd_benchmark(args) -> None:
    from .benchmark import benchmark, summarize
    from .io import load_players

    if args.league:
        # Your league's players, with its extra sources merged into the consensus.
        state = load_state(args.players, args.league, None)
        league, players = state.league, state.players
        print(f"players: {args.players or 'from ' + args.league} ({len(players)})")
    else:
        league = None
        players = load_players(args.players or "data/sample_players.csv")
        print(f"players: {args.players or 'data/sample_players.csv (synthetic sample)'}")
    if league is None or league.num_teams != 10:
        from .models import League, TeamProfile

        league = League([TeamProfile(f"T{i}") for i in range(10)], 0)
    from .benchmark import rank_vs_projection

    print(rank_vs_projection(players))
    print(f"{args.drafts} drafts x {len(args.policies)} policies, recommender sims={args.sims}")
    results = benchmark(players, league, args.drafts, args.sims, args.candidates, args.policies)
    print()
    print(summarize(results))


def _league_id(text: str) -> int:
    """Accept a bare id or any ESPN URL containing leagueId=..."""
    import re

    m = re.search(r"leagueId=(\d+)", text) or re.fullmatch(r"\s*(\d+)\s*", text)
    if not m:
        raise argparse.ArgumentTypeError(f"not a league id or league URL: {text!r}")
    return int(m.group(1))


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="fantasydraft", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    players_help = "players CSV or Google Sheets link (default: league.json's \"players\", else data/players.csv)"

    def files(p):
        p.add_argument("--players", help=players_help)
        p.add_argument("--league", default="league.json")
        p.add_argument("--picks", default="picks.txt")
        p.add_argument("--seed", type=int, default=0)

    def sim(p):
        p.add_argument("--sims", type=int, default=100, help="Monte Carlo runs per candidate")
        p.add_argument("--candidates", type=int, default=8)

    def espn(p):
        p.add_argument("--league-id", type=_league_id, required=True,
                       help="ESPN league id, or your league URL")
        p.add_argument("--season", type=int, required=True, help="e.g. 2027 for the 2026-27 season")
        p.add_argument("--espn-s2", default=os.environ.get("ESPN_S2"))
        p.add_argument("--swid", default=os.environ.get("ESPN_SWID"))
        p.add_argument("--dump", help="also save the raw ESPN JSON here")

    p = sub.add_parser("recommend", help="rank options for my next pick")
    files(p), sim(p)
    p.set_defaults(func=cmd_recommend)

    p = sub.add_parser("availability", help="odds top players last to my next picks")
    files(p)
    p.add_argument("--sims", type=int, default=300)
    p.add_argument("--top", type=int, default=30)
    p.set_defaults(func=cmd_availability)

    p = sub.add_parser("import-picks", help="build picks.txt from text copied off a draft results page")
    p.add_argument("source", help="text file with the pasted results, or - for stdin")
    files(p)
    p.set_defaults(func=cmd_import_picks)

    p = sub.add_parser("analyze", help="opponents' draft tendencies so far")
    files(p)
    p.set_defaults(func=cmd_analyze)

    p = sub.add_parser("draft", help="interactive tracker: type picks and autodraft switches, see recommendations")
    files(p)
    p.add_argument("--sims", type=int, default=60, help="Monte Carlo runs per candidate (lower = faster)")
    p.add_argument("--candidates", type=int, default=8)
    p.set_defaults(func=cmd_draft)

    p = sub.add_parser("new-league", help="create a league file (e.g. for an ESPN mock draft)")
    p.add_argument("out", nargs="?", default="mock.json")
    p.add_argument("--teams", type=int, default=10)
    p.add_argument("--my-pick", type=int, required=True, help="your draft position (1 = first)")
    p.add_argument("--players", help="players CSV or Google Sheets link")
    p.add_argument("--names", help="comma-separated team names in draft order")
    p.add_argument("--strategy", default="unknown", choices=["unknown", "auto", "adp", "rank", "points"],
                   help="saved strategy for the other teams (unknown = learn from their picks)")
    p.add_argument("--noise", type=float, default=0.2)
    p.add_argument("--like", default="league.json",
                   help="copy players, sources and scoring from this league file if it exists (default league.json)")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_new_league)

    p = sub.add_parser("watch", help="re-run recommendations whenever the picks file changes")
    files(p), sim(p)
    p.add_argument("--interval", type=float, default=1.0)
    p.set_defaults(func=cmd_watch)

    p = sub.add_parser("mock", help="write a simulated draft into the picks file")
    files(p)
    p.add_argument("--delay", type=float, default=2.0, help="seconds between picks")
    p.add_argument("--until", type=int, help="stop after this many picks")
    p.add_argument("--auto-me", action="store_true", help="simulate my picks too (no prompts)")
    p.set_defaults(func=cmd_mock)

    p = sub.add_parser("fetch-players", help="download ESPN rankings/projections to CSV")
    espn(p)
    p.add_argument("--out", default="data/players.csv")
    p.add_argument("--limit", type=int, default=400)
    p.add_argument("--rank-type", default="STANDARD", help="ESPN draft rank list (STANDARD, POINTS, ...)")
    p.add_argument("--defaults", action="store_true",
                   help="use ESPN's default league (before your league is activated)")
    p.add_argument("--force", action="store_true", help="overwrite --out even if it has your own columns")
    p.set_defaults(func=cmd_fetch_players)

    p = sub.add_parser("fetch-league", help="create/refresh league.json from ESPN settings")
    espn(p)
    p.add_argument("--out", default="league.json")
    p.set_defaults(func=cmd_fetch_league)

    p = sub.add_parser("ui", help="draft dashboard in your browser (live from the ESPN draft room, or manual entry)")
    files(p)
    p.add_argument("--sims", type=int, default=60, help="Monte Carlo runs per candidate (lower = faster)")
    p.add_argument("--candidates", type=int, default=8)
    p.add_argument("--out", default="draft_feed.jsonl", help="raw draft-room log (troubleshooting)")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--no-browser", action="store_true", help="don't open the dashboard automatically")
    p.add_argument("--tab", action="store_true", help="open in a normal browser tab instead of its own window")
    p.set_defaults(func=cmd_ui)

    p = sub.add_parser("mock-draft", help="ESPN mock in one step: league file for the room + dashboard")
    p.add_argument("--teams", type=int, required=True, help="number of teams in the mock room")
    p.add_argument("--pick", type=int, required=True, help="your draft position in the room")
    p.add_argument("--league", default="mock.json", help="league file to (re)write for this mock")
    p.add_argument("--like", default="league.json", help="copy players, sources and scoring from this file")
    p.add_argument("--sims", type=int, default=60)
    p.set_defaults(func=cmd_mock_draft)

    p = sub.add_parser("review", help="after a draft: your picks vs ESPN's board, and how the gambles came out")
    p.add_argument("picks", nargs="?", help="picks file (default: the latest in mocks/)")
    p.add_argument("--league", help="league file (default: the one saved with the mock, else mock.json)")
    p.add_argument("--players", help=players_help)
    p.set_defaults(func=cmd_review)

    p = sub.add_parser("model-check", help="after a draft: does learning each team's habits improve the predictions?")
    p.add_argument("picks", nargs="?", help="picks file (default: the latest in mocks/)")
    p.add_argument("--league", help="league file (default: the one saved with the mock, else mock.json)")
    p.add_argument("--players", help=players_help)
    p.add_argument("--sims", type=int, default=150)
    p.set_defaults(func=cmd_model_check)

    p = sub.add_parser("study", help="all your mocks: how autodraft and people actually pick (paste the output)")
    p.add_argument("picks", nargs="*", help="picks files (default: every mock in mocks/)")
    p.add_argument("--folder", default="mocks")
    p.add_argument("--players", help=players_help)
    p.set_defaults(func=cmd_study)

    p = sub.add_parser("room", help="follow an ESPN draft room live via the browser userscript (writes picks.txt)")
    files(p)
    p.add_argument("--out", default="capture.jsonl", help="raw event log (for troubleshooting)")
    p.add_argument("--port", type=int, default=8765)
    p.set_defaults(func=cmd_room)

    p = sub.add_parser("sources", help="check extra projection sources; who ESPN under/overrates")
    p.add_argument("--league", default="league.json")
    p.add_argument("--players", help="players CSV or sheet link (default: league file's)")
    p.add_argument("--top", type=int, default=150, help="consider the top N players by consensus")
    p.add_argument("--show", type=int, default=15)
    p.set_defaults(func=cmd_sources)

    p = sub.add_parser("capture-ranks", help="extract the draft room's player rankings from a capture file")
    p.add_argument("capture", help="capture .jsonl recorded while a draft room was open")
    p.add_argument("--out", default="draft_room_ranks.csv")
    p.add_argument("--league", help="league file, to compare with your players list")
    p.add_argument("--players", help="players CSV or sheet link, to compare")
    p.set_defaults(func=cmd_capture_ranks)

    p = sub.add_parser("history", help="how each league member drafted in a past season")
    espn(p)
    p.add_argument("--me", help="your name as on ESPN (so you're left out of the fitting)")
    p.add_argument("--write", metavar="LEAGUE_FILE", help="merge the results into this league file's managers")
    p.set_defaults(func=cmd_history)

    p = sub.add_parser("benchmark", help="simulated drafts: how much each pick policy gains over autodraft")
    p.add_argument("--players", help="players CSV or sheet link (default: sample data)")
    p.add_argument("--league", help="league file for roster settings (default: ESPN defaults, 10 teams)")
    p.add_argument("--drafts", type=int, default=20)
    p.add_argument("--sims", type=int, default=30)
    p.add_argument("--candidates", type=int, default=8)
    p.add_argument("--policies", nargs="+", default=["auto", "points", "greedy", "model"])
    p.set_defaults(func=cmd_benchmark)

    p = sub.add_parser("replay", help="replay a dashboard feed log and write a shareable report of what happened")
    p.add_argument("log", nargs="?", default="draft_feed.jsonl", help="the dashboard's feed log (ui --out)")
    p.add_argument("--league", default="league.json")
    p.add_argument("--players", help=players_help)
    p.add_argument("--out", default="replay_report.txt")
    p.add_argument("--session", type=int, default=0,
                   help="which draft in the log (1 = first; default: the last)")
    p.set_defaults(func=cmd_replay)

    p = sub.add_parser("capture", help="record ESPN draft-room traffic sent by the browser userscript")
    p.add_argument("--out", default="capture.jsonl")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--summary", metavar="FILE",
                   help="don't capture; write a redacted, shareable summary of --out to FILE")
    p.set_defaults(func=cmd_capture)

    p = sub.add_parser("live", help="mirror the live ESPN draft into the picks file")
    espn(p)
    p.add_argument("--players", help=players_help)
    p.add_argument("--league", default="league.json")
    p.add_argument("--picks", default="picks.txt")
    p.add_argument("--interval", type=float, default=3.0)
    p.set_defaults(func=cmd_live)

    args = ap.parse_args(argv)
    try:
        args.func(args)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
