import random
from pathlib import Path

import pytest

from fantasydraft.analyze import analyze
from fantasydraft.drafters import choose_pick, order_for
from fantasydraft.espn import EspnClient, parse_player
from fantasydraft.io import load_players, load_state, parse_positions, resolve_player
from fantasydraft.lineup import best_lineup, roster_value
from fantasydraft.models import DraftState, League, Player, TeamProfile
from fantasydraft.recommend import recommend
from fantasydraft.simulate import Simulator, availability

ROOT = Path(__file__).resolve().parent.parent
SAMPLE = ROOT / "data" / "sample_players.csv"


def P(name, pos, pts, rank=None, adp=None):
    return Player(name, name, parse_positions(pos), pts,
                  float(rank) if rank else float("inf"), float(adp) if adp else float("inf"))


def league(n=4, my=0, strategies=None, slots=None, **kw):
    strategies = strategies or ["auto"] * n
    return League([TeamProfile(f"T{i}", s, noise=0.15) for i, s in enumerate(strategies)], my,
                  roster_slots=slots or {"PG": 1, "C": 1, "UTIL": 1, "BE": 1}, **kw)


def test_snake_order():
    lg = league(n=3)
    assert [lg.team_for_pick(p) for p in range(9)] == [0, 1, 2, 2, 1, 0, 0, 1, 2]
    assert lg.picks_for_team(0) == [0, 5, 6, 11]
    assert lg.total_picks == 12


def test_lineup_needs_matching_not_greedy_slotting():
    # Greedy slotting would put the PG/C into PG and leave C empty.
    slots = ["PG", "C"]
    a, b = P("a", "PG/C", 100), P("b", "PG", 90)
    starters, bench = best_lineup([a, b], slots)
    assert {p.name for p in starters} == {"a", "b"} and not bench


def test_roster_value_bench_weight():
    slots = ["C"]
    roster = [P("a", "C", 100), P("b", "C", 50), P("c", "PG", 40)]
    assert roster_value(roster, slots, 0.5) == pytest.approx(100 + 0.5 * 90)


def test_auto_drafter_takes_top_rank():
    players = [P("a", "PG", 10, rank=2), P("b", "C", 5, rank=1)]
    pick = choose_pick(TeamProfile("x", "auto"), order_for("auto", players), random.Random(0))
    assert pick.name == "b"


def test_favorites_pull_players_forward():
    players = [P(f"p{i}", "PG", 100 - i, rank=i + 1, adp=i + 1) for i in range(20)]
    prof = TeamProfile("x", "adp", noise=0.0, favorites={"p9": 9.5})
    assert choose_pick(prof, order_for("adp", players), random.Random(0)).name == "p9"


def _tiny_state(strategies, picks=(), my=0):
    players = {p.id: p for p in [
        P("pg1", "PG", 100, 1, 1), P("c1", "C", 95, 2, 2), P("pg2", "PG", 90, 3, 3),
        P("c2", "C", 60, 4, 4), P("pg3", "PG", 55, 5, 5), P("c3", "C", 30, 6, 6),
        P("u1", "SF", 50, 7, 7), P("u2", "SF", 45, 8, 8), P("u3", "SF", 20, 9, 9),
        P("u4", "SF", 10, 10, 10),
    ]}
    return DraftState(league(n=len(strategies), my=my, strategies=strategies), players, list(picks))


def test_availability_deterministic_with_auto_opponents():
    state = _tiny_state(["auto", "auto"], my=1)
    # T0 takes pg1 at pick 0, my picks are 1 and 2; opponents take nothing in between.
    probs = availability(state, sims=5)
    assert probs["pg1"] == [0.0, 0.0, 0.0, 0.0]
    assert probs["c1"][0] == 1.0 and probs["c1"][1] == 1.0
    # T0 picks again at 3 and 4 (taking c1 and pg2 if I pass on them)
    assert probs["c1"][2] == 0.0


def test_recommend_prefers_scarce_position():
    # Pick 1 of a 2-team draft. PGs are deep (100, 98, 96) but centers fall off
    # a cliff (95 -> 40), so taking the C first beats taking the top scorer.
    players = {p.id: p for p in [
        P("pg1", "PG", 100, 1), P("c1", "C", 95, 2), P("pg2", "PG", 98, 3),
        P("pg3", "PG", 96, 4), P("c2", "C", 40, 5), P("x1", "SF", 10, 6),
        P("x2", "SF", 9, 7), P("x3", "SF", 8, 8),
    ]}
    lg = league(n=2, strategies=["auto", "auto"], slots={"PG": 1, "C": 1, "BE": 2})
    recs = recommend(DraftState(lg, players), candidates=4, sims=3)
    assert recs[0].player.name == "c1"
    assert recs[0].p_avail_now == 1.0
    assert max(recs, key=lambda r: r.marginal_now).player.name == "pg1"


def test_simulation_completes_draft():
    state = load_state(SAMPLE, ROOT / "examples" / "league.json", None)
    result = Simulator(state).run(random.Random(1))
    assert len(result.my_roster) == state.league.rounds
    assert len(result.taken_at) == state.league.total_picks - state.league.rounds


def test_analyze_flags_autodrafter():
    state = _tiny_state(["auto", "points"])
    sim = Simulator(state)
    rng = random.Random(0)
    taken: set[str] = set()
    for pick in range(state.league.total_picks):
        prof = state.league.teams[state.league.team_for_pick(pick)]
        choice = choose_pick(prof, sim.top_untaken(prof.strategy, taken, 30), rng, pick)
        state.picks.append(choice.id)
        taken.add(choice.id)
    tendencies = analyze(state)
    assert tendencies[0].suggested == "auto"


def test_resolve_player_names():
    players = load_players(SAMPLE)
    assert players[resolve_player("player 001", players)].name == "Player 001"
    with pytest.raises(ValueError):
        resolve_player("Player 0", players)


def test_parse_espn_player_and_picks():
    raw = {
        "id": 3032977,
        "player": {
            "id": 3032977, "fullName": "Some Star", "defaultPositionId": 3,
            "eligibleSlots": [2, 3, 6, 8, 10, 11, 12, 13], "proTeamId": 15,
            "draftRanksByRankType": {"STANDARD": {"rank": 4}},
            "ownership": {"averageDraftPosition": 3.7},
            "stats": [
                {"seasonId": 2026, "statSourceId": 0, "statSplitTypeId": 0, "appliedTotal": 1.0},
                {"seasonId": 2027, "statSourceId": 1, "statSplitTypeId": 0, "appliedTotal": 3400.5},
            ],
        },
    }
    p = parse_player(raw, 2027)
    assert p.positions == frozenset({"SF", "PF"})
    assert (p.proj_points, p.espn_rank, p.adp) == (3400.5, 4.0, 3.7)

    league = {"draftDetail": {"picks": [
        {"overallPickNumber": 2, "teamId": 5, "playerId": 9, "autoDraftTypeId": 1},
        {"overallPickNumber": 1, "teamId": 3, "playerId": 8, "autoDraftTypeId": 0},
        {"overallPickNumber": 3, "teamId": 1, "playerId": -1},
    ]}}
    picks = EspnClient.picks(league)
    assert [(p.player_id, p.auto) for p in picks] == [("8", False), ("9", True)]


def test_sheet_links_become_csv_exports():
    from fantasydraft.io import sheet_csv_url

    base = "https://docs.google.com/spreadsheets/d/AbC-123_x"
    assert sheet_csv_url(base + "/edit?gid=0#gid=0") == base + "/export?format=csv&gid=0"
    assert sheet_csv_url(base + "/edit#gid=42") == base + "/export?format=csv&gid=42"
    assert sheet_csv_url(base + "/edit") == base + "/export?format=csv"
    assert sheet_csv_url("https://example.com/p.csv") == "https://example.com/p.csv"


def test_my_proj_and_ignore_columns(tmp_path):
    f = tmp_path / "p.csv"
    f.write_text(
        "id,name,positions,proj_points,espn_rank,adp,my_proj,ignore,notes\n"
        '1,A,PG,"1,500.5",1,2,,,\n'
        "2,B,C,1400,2,1,1600,,sleeper\n"
        "3,C,SF,1300,3,3,,x,hurt\n"
        "4,D,SF,,,,,,no projection yet\n"
        ",,,,,,,,\n"
    )
    players = load_players(f)
    assert set(players) == {"1", "2"}
    assert players["1"].proj_points == 1500.5
    assert players["2"].proj_points == 1600
