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
import sys
import time
from pathlib import Path

from .analyze import analyze
from .drafters import CANDIDATE_POOL, choose_pick
from .io import load_log, load_picks, load_state, player_by_rank, resolve_player, resolve_team, save_log, save_picks, save_players
from .models import DraftState
from .recommend import recommend
from .simulate import Simulator, availability


def _table(rows: list[list[str]], header: list[str]) -> str:
    widths = [max(len(str(x)) for x in col) for col in zip(header, *rows)]
    fmt = "  ".join(f"{{:<{w}}}" for w in widths)
    lines = [fmt.format(*header), fmt.format(*["-" * w for w in widths])]
    lines += [fmt.format(*map(str, r)) for r in rows]
    return "\n".join(lines)


def _state(args) -> DraftState:
    return load_state(args.players, args.league, args.picks)


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
        lines.append("Your roster: " + ", ".join(f"{p.name} ({'/'.join(sorted(p.positions))})" for p in roster))
    return "\n".join(lines)


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
            f"{r.p_avail_now:.0%}",
            f"{r.p_avail_after:.0%}",
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
         *(f"{x:.0%}" for x in probs[p.id][:3])]
        for p in top
    ]
    print()
    print(_table(rows, ["player", "pos", "proj", *(f"P(avail @#{m + 1})" for m in mine)]))


def cmd_import_picks(args) -> None:
    """Turn text copied from a draft results page into a picks file."""
    from .io import extract_picks, load_players, players_source

    players = load_players(args.players or players_source(args.league))
    text = sys.stdin.read() if args.source == "-" else Path(args.source).read_text(encoding="utf-8")
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
    state = _state(args)
    rows = [
        [t.team, t.picks, t.auto_picks or "", f"{t.top_rank_rate:.0%}", f"{t.top_points_rate:.0%}",
         f"{t.reach_vs_adp:+.1f}", f"{t.reach_vs_rank:+.1f}", t.suggested]
        for t in analyze(state)
    ]
    print(_table(rows, ["team", "own picks", "auto picks", "took top ESPN rank", "took top proj",
                        "ADP - pick", "rank - pick", "suggested strategy"]))
    print("\n'ADP - pick' > 0 means the team takes players earlier than ADP (reaches).")
    print("Picks made while a team was marked @auto are left out of its stats.")


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
            state = _state(args)
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
        "bench_weight": 0.4,
        "roster_slots": {"PG": 1, "SG": 1, "SF": 1, "PF": 1, "C": 1, "G": 1, "F": 1, "UTIL": 3, "BE": 3},
        "teams": teams,
    }
    out.write_text(json.dumps(cfg, indent=2) + "\n")
    print(f"wrote {out}: {args.teams} teams, you pick #{args.my_pick}, others '{args.strategy}'")


def cmd_watch(args) -> None:
    last = None
    while True:
        try:
            mtime = (Path(args.picks).stat().st_mtime if Path(args.picks).exists() else 0,
                     Path(args.league).stat().st_mtime)
            if mtime != last:
                last = mtime
                os.system("cls" if os.name == "nt" else "clear")
                _print_recommendations(_state(args), args)
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
            choice = choose_pick(team, sim.top_untaken(team.strategy, taken, CANDIDATE_POOL), rng, pick)
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
    out = Path(args.out)
    existing = json.loads(out.read_text()) if out.exists() else {}
    old = {t["name"]: t for t in existing.get("teams", [])}
    cfg = {
        "players": existing.get("players", "data/players.csv"),
        "my_team": existing.get("my_team", 1),
        "bench_weight": existing.get("bench_weight", 0.4),
        "roster_slots": client.roster_slots(data),
        "espn_team_ids": client.pick_order(data),
        "teams": [old.get(names[tid], {"name": names[tid], "strategy": "adp", "noise": 0.15})
                  for tid in client.pick_order(data)],
    }
    out.write_text(json.dumps(cfg, indent=2))
    print(f"wrote {out} — set 'my_team' and each team's 'strategy' before the draft")


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
                on_auto: set[int] = set()
                for p in picks:
                    if p.player_id not in state.players:
                        continue
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


def cmd_capture(args) -> None:
    from .capture import serve, summarize

    if args.summary:
        text = summarize(args.out)
        Path(args.summary).write_text(text, encoding="utf-8")
        print(text[:4000])
        print(f"\n(full summary written to {args.summary})")
    else:
        serve(args.out, args.port)


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
    p.add_argument("--strategy", default="adp", choices=["auto", "adp", "rank", "points"],
                   help="saved strategy for the other teams")
    p.add_argument("--noise", type=float, default=0.2)
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
    p.set_defaults(func=cmd_fetch_players)

    p = sub.add_parser("fetch-league", help="create/refresh league.json from ESPN settings")
    espn(p)
    p.add_argument("--out", default="league.json")
    p.set_defaults(func=cmd_fetch_league)

    p = sub.add_parser("room", help="follow an ESPN draft room live via the browser userscript (writes picks.txt)")
    files(p)
    p.add_argument("--out", default="capture.jsonl", help="raw event log (for troubleshooting)")
    p.add_argument("--port", type=int, default=8765)
    p.set_defaults(func=cmd_room)

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
