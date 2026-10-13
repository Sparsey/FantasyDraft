"""Command line interface.

Typical draft-day setup (two terminals):

    python -m fantasydraft live  --league-id 123 --season 2027 ...   # ESPN -> picks.txt
    python -m fantasydraft watch --players data/players.csv --league league.json --picks picks.txt

`watch` also works with a hand-edited picks.txt if the ESPN feed breaks, and
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
from .io import load_picks, load_state, save_picks, save_players
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
        lines.append(f"Your next pick: #{mine[0] + 1} ({until} picks away)")
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


def cmd_analyze(args) -> None:
    state = _state(args)
    rows = [
        [t.team, t.picks, f"{t.top_rank_rate:.0%}", f"{t.top_points_rate:.0%}",
         f"{t.reach_vs_adp:+.1f}", f"{t.reach_vs_rank:+.1f}", t.suggested]
        for t in analyze(state)
    ]
    print(_table(rows, ["team", "picks", "took top ESPN rank", "took top proj",
                        "ADP - pick", "rank - pick", "suggested strategy"]))
    print("\n'ADP - pick' > 0 means the team takes players earlier than ADP (reaches).")


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
    from .espn import EspnClient

    client = EspnClient(args.league_id, args.season, args.espn_s2, args.swid)
    if args.dump:
        Path(args.dump).write_text(json.dumps(client.players_raw(args.limit, args.rank_type), indent=1))
    players = client.players(args.limit, args.rank_type)
    save_players(players, args.out)
    print(f"wrote {len(players)} players to {args.out}")


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
                ids = [p.player_id for p in picks if p.player_id in state.players]
                save_picks(ids, state.players, args.picks)
                names = client.team_names(data)
                auto_teams = sorted({names.get(p.team_id, str(p.team_id)) for p in picks if p.auto})
                print(f"{len(picks)} picks synced" + (f"; autodraft picks by: {', '.join(auto_teams)}" if auto_teams else ""))
        except Exception as e:
            print(f"error: {e}", file=sys.stderr)
        time.sleep(args.interval)


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
        p.add_argument("--league-id", type=int, required=True)
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

    p = sub.add_parser("analyze", help="opponents' draft tendencies so far")
    files(p)
    p.set_defaults(func=cmd_analyze)

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
    p.set_defaults(func=cmd_fetch_players)

    p = sub.add_parser("fetch-league", help="create/refresh league.json from ESPN settings")
    espn(p)
    p.add_argument("--out", default="league.json")
    p.set_defaults(func=cmd_fetch_league)

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
